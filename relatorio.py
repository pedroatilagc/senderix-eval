import math
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from comparador import (
    avaliar_limiares,
    carregar_execucao,
    carregar_limiares,
    comparar,
    media_notas,
    taxa_acerto,
)
from contratos import Categoria, Comparacao, ExecucaoAvaliacao, Limiares, MudancaCaso, ResultadoCaso, Status
from executor import carregar_dataset

RAIZ = Path(__file__).parent
LIMITE_HISTORICO = 10
CATEGORIAS: tuple[Categoria, ...] = get_args(Categoria)
ROTULOS_CATEGORIA = {
    "interessado": "Interessado",
    "duvida": "Dúvida",
    "opt_out": "Opt-out",
    "reclamacao": "Reclamação",
    "fora_de_contexto": "Fora de contexto",
}

Serie = Literal["base", "nova", "neutra"]
Tom = Literal["bom", "ruim", "neutro"]


class Provedor(BaseModel):
    chave: str
    nome: str
    cor: str


PROVEDORES = {
    "openai": Provedor(chave="openai", nome="OpenAI", cor="#11A68B"),
    "anthropic": Provedor(chave="anthropic", nome="Anthropic", cor="#B87A2E"),
    "google": Provedor(chave="google", nome="Google", cor="#1096E9"),
    "deepseek": Provedor(chave="deepseek", nome="DeepSeek", cor="#8F63B8"),
}
PROVEDOR_GENERICO = Provedor(chave="generico", nome="Outro", cor="#9CA3AF")
COR_NEUTRA = "#4B5563"


class IdentidadeModelo(BaseModel):
    modelo: str
    nome: str
    prompt_versao: str
    provedor: Provedor
    cor: str


def identidade(execucao: ExecucaoAvaliacao) -> IdentidadeModelo:
    chave, _, nome = execucao.modelo.partition("/")
    provedor = PROVEDORES.get(chave, PROVEDOR_GENERICO)
    return IdentidadeModelo(
        modelo=execucao.modelo, nome=nome or chave, prompt_versao=execucao.prompt_versao,
        provedor=provedor, cor=provedor.cor,
    )


def identidades(base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao) -> tuple[IdentidadeModelo, IdentidadeModelo]:
    """Mesma cor nos dois lados (mesmo modelo/provedor): a base volta para o cinza neutro."""
    id_base, id_nova = identidade(base), identidade(nova)
    if id_base.cor == id_nova.cor:
        id_base = id_base.model_copy(update={"cor": COR_NEUTRA})
    return id_base, id_nova


def fmt(valor: float, casas: int = 1) -> str:
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_data(iso: str) -> str:
    return f"{datetime.fromisoformat(iso):%d/%m %H:%M}"


def casas_custo(valor: float) -> int:
    return 5 if valor < 0.01 else 4


class Metricas(BaseModel):
    executado_em: str
    prompt_versao: str
    taxa_acerto: float
    conformidade_formato: float
    nota_media: float | None
    latencia_p50: float
    latencia_p95: float
    custo_total: float | None
    custo_juiz: float | None
    tokens_entrada: int
    tokens_saida: int


def percentil_95(valores: list[float]) -> float:
    if len(valores) < 2:
        return valores[0]
    return statistics.quantiles(valores, n=20)[18]


def soma_opcional(valores: list[float | None]) -> float | None:
    presentes = [v for v in valores if v is not None]
    return sum(presentes) if presentes else None


def calcular_metricas(execucao: ExecucaoAvaliacao) -> Metricas:
    r = execucao.resultados
    latencias = [x.latencia_ms for x in r]
    return Metricas(
        executado_em=execucao.executado_em,
        prompt_versao=execucao.prompt_versao,
        taxa_acerto=taxa_acerto(r),
        conformidade_formato=sum(x.formato_valido for x in r) / len(r) * 100,
        nota_media=media_notas(r),
        latencia_p50=statistics.median(latencias),
        latencia_p95=percentil_95(latencias),
        custo_total=soma_opcional([x.custo_usd for x in r]),
        custo_juiz=soma_opcional([x.custo_juiz_usd for x in r]),
        tokens_entrada=sum(x.tokens_entrada for x in r),
        tokens_saida=sum(x.tokens_saida for x in r),
    )


def carregar_historico(nova: ExecucaoAvaliacao) -> list[ExecucaoAvaliacao]:
    execucoes = [carregar_execucao(str(p)) for p in sorted((RAIZ / "resultados").glob("*.json"))]
    mesmas = [
        e for e in execucoes
        if e.modelo == nova.modelo
        and e.dataset_versao == nova.dataset_versao
        and e.executado_em <= nova.executado_em
    ]
    mesmas.sort(key=lambda e: e.executado_em)
    return mesmas[-LIMITE_HISTORICO:]


class Variacao(BaseModel):
    texto: str
    seta: Literal["▲", "▼", "="]
    tom: Tom

class StatCard(BaseModel):
    rotulo: str
    valor: float | None
    valor_fmt: str
    casas: int
    prefixo: str = ""
    sufixo: str = ""
    variacao: Variacao | None
    detalhe: str


def variacao(base: float | None, nova: float | None, texto_delta: str, maior_melhor: bool) -> Variacao | None:
    if base is None or nova is None:
        return None
    delta = nova - base
    if abs(delta) < 1e-9:
        return Variacao(texto="sem variação", seta="=", tom="neutro")
    melhorou = (delta > 0) == maior_melhor
    return Variacao(texto=texto_delta, seta="▲" if delta > 0 else "▼", tom="bom" if melhorou else "ruim")


def sinal(delta: float) -> str:
    return "+" if delta >= 0 else "−"


def montar_kpis(base: Metricas, nova: Metricas) -> list[StatCard]:
    def delta_pp(a: float, b: float) -> str:
        return f"{sinal(b - a)}{fmt(abs(b - a))} pp"

    cards = [
        StatCard(
            rotulo="Taxa de acerto", valor=nova.taxa_acerto, valor_fmt=fmt(nova.taxa_acerto), casas=1, sufixo="%",
            variacao=variacao(base.taxa_acerto, nova.taxa_acerto, delta_pp(base.taxa_acerto, nova.taxa_acerto), True),
            detalhe=f"base {fmt(base.taxa_acerto)}%",
        ),
        StatCard(
            rotulo="Formato válido", valor=nova.conformidade_formato, valor_fmt=fmt(nova.conformidade_formato),
            casas=1, sufixo="%",
            variacao=variacao(
                base.conformidade_formato, nova.conformidade_formato,
                delta_pp(base.conformidade_formato, nova.conformidade_formato), True,
            ),
            detalhe=f"base {fmt(base.conformidade_formato)}%",
        ),
    ]

    nota_delta = (
        f"{sinal(nova.nota_media - base.nota_media)}{fmt(abs(nova.nota_media - base.nota_media), 2)}"
        if base.nota_media is not None and nova.nota_media is not None else ""
    )
    cards.append(StatCard(
        rotulo="Nota do resumo", valor=nova.nota_media,
        valor_fmt=fmt(nova.nota_media, 2) if nova.nota_media is not None else "—", casas=2, sufixo=" /5",
        variacao=variacao(base.nota_media, nova.nota_media, nota_delta, True),
        detalhe=f"base {fmt(base.nota_media, 2)}" if base.nota_media is not None else "juiz LLM, escala 1–5",
    ))

    lat_delta = nova.latencia_p50 - base.latencia_p50
    cards.append(StatCard(
        rotulo="Latência P50", valor=nova.latencia_p50, valor_fmt=fmt(nova.latencia_p50, 0), casas=0, sufixo=" ms",
        variacao=variacao(base.latencia_p50, nova.latencia_p50, f"{sinal(lat_delta)}{fmt(abs(lat_delta), 0)} ms", False),
        detalhe=f"P95 {fmt(nova.latencia_p95, 0)} ms",
    ))

    custo_delta = ""
    if base.custo_total and nova.custo_total is not None:
        pct = (nova.custo_total - base.custo_total) / base.custo_total * 100
        custo_delta = f"{sinal(pct)}{fmt(abs(pct))}%"
    casas = casas_custo(nova.custo_total) if nova.custo_total is not None else 4
    cards.append(StatCard(
        rotulo="Custo da execução", valor=nova.custo_total,
        valor_fmt=fmt(nova.custo_total, casas) if nova.custo_total is not None else "—", casas=casas, prefixo="US$ ",
        variacao=variacao(base.custo_total or None, nova.custo_total, custo_delta, False),
        detalhe=(
            f"base US$ {fmt(base.custo_total, casas)}" if base.custo_total is not None else "sem custo do juiz"
        ),
    ))
    return cards

class Motivo(BaseModel):
    nivel: Status
    texto: str

def separar_motivos(motivos: list[str]) -> list[Motivo]:
    saida = []
    for motivo in motivos:
        achado = re.match(r"^\[(ok|aviso|critico)\]\s*(.*)$", motivo)
        if achado:
            saida.append(Motivo(nivel=achado.group(1), texto=achado.group(2)))
        else:
            saida.append(Motivo(nivel="aviso", texto=motivo))
    return saida

class LinhaDica(BaseModel):
    rotulo: str
    valor: str
    serie: Serie = "neutra"
    provedor: str | None = None

class Dica(BaseModel):
    titulo: str
    linhas: list[LinhaDica]

class Tick(BaseModel):
    pos: float
    rotulo: str


class Barra(BaseModel):
    x: float
    y: float
    largura: float
    altura: float
    serie: Serie
    indice: int


class Alvo(BaseModel):
    x: float
    largura: float
    dica: Dica


class LinhaRef(BaseModel):
    y: float
    rotulo: str


class GraficoBarras(BaseModel):
    barras: list[Barra]
    alvos: list[Alvo]
    ticks_y: list[Tick]
    rotulos_x: list[Tick]
    linhas_ref: list[LinhaRef] = []


class Ponto(BaseModel):
    x: float
    y: float
    atual: bool


class PainelTendencia(BaseModel):
    titulo: str
    pontos: list[Ponto]
    trajeto: str
    ticks_y: list[Tick]
    alvos: list[Alvo]
    vazio: str | None = None


class Tendencia(BaseModel):
    paineis: list[PainelTendencia]
    rotulos_x: list[Tick]
    quantidade: int


class SegmentoToken(BaseModel):
    x: float
    largura: float
    tipo: Literal["entrada", "saida"]
    dica: Dica


class LinhaToken(BaseModel):
    modelo: IdentidadeModelo
    serie: Serie
    total_fmt: str
    segmentos: list[SegmentoToken]


def teto_redondo(valor: float, divisoes: int = 4) -> tuple[float, float]:
    """Devolve (máximo do eixo, passo) com passos 1/2/2,5/5 × 10^k."""
    if valor <= 0:
        return 1.0, 0.25
    bruto = valor / divisoes
    potencia = 10 ** math.floor(math.log10(bruto))
    for mult in (1, 2, 2.5, 5, 10):
        passo = mult * potencia
        if passo * divisoes >= valor:
            return passo * divisoes, passo
    return 10 * potencia * divisoes, 10 * potencia


def y_pct(valor: float, minimo: float, maximo: float) -> float:
    return 100 - (valor - minimo) / (maximo - minimo) * 100


def linha_modelo(m: IdentidadeModelo, valor: str, serie: Serie) -> LinhaDica:
    return LinhaDica(rotulo=m.nome, valor=valor, serie=serie, provedor=m.provedor.chave)


def grafico_categorias(
    c: Comparacao, nova: ExecucaoAvaliacao, id_base: IdentidadeModelo, id_nova: IdentidadeModelo
) -> GraficoBarras:
    categorias = [cat for cat in CATEGORIAS if cat in c.acuracia_por_categoria]
    casos = {cat: sum(r.categoria_esperada == cat for r in nova.resultados) for cat in categorias}
    slot = 100 / len(categorias)
    largura, vao = slot * 0.24, slot * 0.03
    barras, alvos, rotulos = [], [], []
    for i, cat in enumerate(categorias):
        antes, depois = c.acuracia_por_categoria[cat]
        centro = slot * i + slot / 2
        for j, (serie, valor) in enumerate((("base", antes), ("nova", depois))):
            x = centro - largura - vao / 2 if serie == "base" else centro + vao / 2
            barras.append(Barra(x=x, y=y_pct(valor, 0, 100), largura=largura, altura=valor, serie=serie, indice=i * 2 + j))
        alvos.append(Alvo(x=slot * i, largura=slot, dica=Dica(
            titulo=f"{ROTULOS_CATEGORIA[cat]} · {casos[cat]} casos",
            linhas=[
                linha_modelo(id_base, f"{fmt(antes)}%", "base"),
                linha_modelo(id_nova, f"{fmt(depois)}%", "nova"),
                LinhaDica(rotulo="Variação", valor=f"{sinal(depois - antes)}{fmt(abs(depois - antes))} pp"),
            ],
        )))
        rotulos.append(Tick(pos=centro, rotulo=ROTULOS_CATEGORIA[cat]))
    ticks = [Tick(pos=y_pct(v, 0, 100), rotulo=f"{v}%") for v in (0, 25, 50, 75, 100)]
    return GraficoBarras(barras=barras, alvos=alvos, ticks_y=ticks, rotulos_x=rotulos)


class VisaoLatencia(BaseModel):
    chave: Literal["nova", "base", "comparar"]
    rotulo: str
    modelo: IdentidadeModelo | None
    grafico: GraficoBarras


class EscalaLatencia(BaseModel):
    maximo: float
    ticks: list[Tick]
    rotulos_x: list[Tick]
    slot: float


def escala_latencia(ids: list[str], base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao) -> EscalaLatencia:
    """Mesmo eixo Y nas três visões, para a troca de aba não enganar a comparação."""
    maximo, passo = teto_redondo(max(r.latencia_ms for r in [*base.resultados, *nova.resultados]))
    slot = 100 / len(ids)
    n = round(maximo / passo)
    return EscalaLatencia(
        maximo=maximo,
        ticks=[Tick(pos=y_pct(passo * k, 0, maximo), rotulo=fmt(passo * k / 1000, 1) + " s") for k in range(n + 1)],
        rotulos_x=[
            Tick(pos=slot * i + slot / 2, rotulo=id_caso.split("-")[-1])
            for i, id_caso in enumerate(ids) if i == 0 or (i + 1) % 5 == 0
        ],
        slot=slot,
    )


def linhas_resultado(r: ResultadoCaso) -> list[LinhaDica]:
    return [
        LinhaDica(rotulo="Categoria", valor=r.categoria_obtida or "inválida"),
        LinhaDica(rotulo="Resultado", valor="acertou" if r.acertou else "errou"),
    ]


def latencia_individual(
    ids: list[str], execucao: ExecucaoAvaliacao, metricas: Metricas, modelo: IdentidadeModelo,
    serie: Serie, escala: EscalaLatencia,
) -> GraficoBarras:
    por_id = {r.id_caso: r for r in execucao.resultados}
    slot, maximo = escala.slot, escala.maximo
    barras, alvos = [], []
    for i, id_caso in enumerate(ids):
        r = por_id.get(id_caso)
        if r is None:
            continue
        barras.append(Barra(
            x=slot * i + slot * 0.18, y=y_pct(r.latencia_ms, 0, maximo), largura=slot * 0.64,
            altura=r.latencia_ms / maximo * 100, serie=serie, indice=i,
        ))
        alvos.append(Alvo(x=slot * i, largura=slot, dica=Dica(
            titulo=id_caso,
            linhas=[linha_modelo(modelo, f"{fmt(r.latencia_ms, 0)} ms", serie), *linhas_resultado(r)],
        )))
    refs = [
        LinhaRef(y=y_pct(metricas.latencia_p50, 0, maximo), rotulo=f"P50 {fmt(metricas.latencia_p50, 0)} ms"),
        LinhaRef(y=y_pct(metricas.latencia_p95, 0, maximo), rotulo=f"P95 {fmt(metricas.latencia_p95, 0)} ms"),
    ]
    return GraficoBarras(barras=barras, alvos=alvos, ticks_y=escala.ticks, rotulos_x=escala.rotulos_x, linhas_ref=refs)


def latencia_comparada(
    ids: list[str], base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao,
    id_base: IdentidadeModelo, id_nova: IdentidadeModelo, escala: EscalaLatencia,
) -> GraficoBarras:
    antes = {r.id_caso: r for r in base.resultados}
    depois = {r.id_caso: r for r in nova.resultados}
    slot, maximo = escala.slot, escala.maximo
    largura, vao = slot * 0.34, slot * 0.04
    barras, alvos = [], []
    for i, id_caso in enumerate(ids):
        centro = slot * i + slot / 2
        linhas = []
        for serie, modelo, r in (("base", id_base, antes.get(id_caso)), ("nova", id_nova, depois.get(id_caso))):
            if r is None:
                continue
            x = centro - largura - vao / 2 if serie == "base" else centro + vao / 2
            barras.append(Barra(
                x=x, y=y_pct(r.latencia_ms, 0, maximo), largura=largura,
                altura=r.latencia_ms / maximo * 100, serie=serie, indice=i,
            ))
            linhas.append(linha_modelo(modelo, f"{fmt(r.latencia_ms, 0)} ms", serie))
        if id_caso in antes and id_caso in depois:
            delta = depois[id_caso].latencia_ms - antes[id_caso].latencia_ms
            linhas.append(LinhaDica(rotulo="Variação", valor=f"{sinal(delta)}{fmt(abs(delta), 0)} ms"))
        alvos.append(Alvo(x=slot * i, largura=slot, dica=Dica(titulo=id_caso, linhas=linhas)))
    return GraficoBarras(barras=barras, alvos=alvos, ticks_y=escala.ticks, rotulos_x=escala.rotulos_x)


def grafico_latencia(
    base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao, m_base: Metricas, m_nova: Metricas,
    id_base: IdentidadeModelo, id_nova: IdentidadeModelo,
) -> list[VisaoLatencia]:
    ids = [r.id_caso for r in nova.resultados]
    escala = escala_latencia(ids, base, nova)
    return [
        VisaoLatencia(chave="nova", rotulo="Nova", modelo=id_nova,
                      grafico=latencia_individual(ids, nova, m_nova, id_nova, "nova", escala)),
        VisaoLatencia(chave="base", rotulo="Base", modelo=id_base,
                      grafico=latencia_individual(ids, base, m_base, id_base, "base", escala)),
        VisaoLatencia(chave="comparar", rotulo="Comparar", modelo=None,
                      grafico=latencia_comparada(ids, base, nova, id_base, id_nova, escala)),
    ]


def painel_tendencia(
    titulo: str, valores: list[float | None], minimo: float, maximo: float, ticks: list[float],
    fmt_valor, xs: list[float], dicas: list[Dica], vazio: str,
) -> PainelTendencia:
    pontos = [
        Ponto(x=x, y=y_pct(v, minimo, maximo), atual=i == len(valores) - 1)
        for i, (x, v) in enumerate(zip(xs, valores)) if v is not None
    ]
    largura = xs[1] - xs[0] if len(xs) > 1 else 100
    return PainelTendencia(
        titulo=titulo,
        pontos=pontos,
        trajeto=" ".join(f"{p.x:.2f},{p.y:.2f}" for p in pontos),
        ticks_y=[Tick(pos=y_pct(t, minimo, maximo), rotulo=fmt_valor(t)) for t in ticks],
        alvos=[Alvo(x=x - largura / 2, largura=largura, dica=d) for x, d in zip(xs, dicas)],
        vazio=None if pontos else vazio,
    )


def grafico_tendencia(historico: list[Metricas], id_nova: IdentidadeModelo) -> Tendencia:
    n = len(historico)
    margem = 6
    xs = [margem + i * (100 - 2 * margem) / (n - 1) for i in range(n)] if n > 1 else [50.0]
    dicas = [
        Dica(
            titulo=f"{fmt_data(m.executado_em)} · prompt {m.prompt_versao}" + (" · atual" if i == n - 1 else ""),
            linhas=[
                LinhaDica(rotulo="Taxa de acerto", valor=f"{fmt(m.taxa_acerto)}%", serie="nova",
                          provedor=id_nova.provedor.chave),
                LinhaDica(
                    rotulo="Nota do resumo",
                    valor=fmt(m.nota_media, 2) if m.nota_media is not None else "sem dado",
                    serie="nova",
                    provedor=id_nova.provedor.chave,
                ),
            ],
        )
        for i, m in enumerate(historico)
    ]

    taxas = [m.taxa_acerto for m in historico]
    piso = max(0, (min(taxas) - 10) // 10 * 10)
    passo_taxa = max(10, (100 - piso) / 4)
    ticks_taxa = [piso + passo_taxa * k for k in range(int((100 - piso) / passo_taxa) + 1)]

    paineis = [
        painel_tendencia("Taxa de acerto", taxas, piso, 100, ticks_taxa,
                         lambda v: f"{fmt(v, 0)}%", xs, dicas, ""),
        painel_tendencia("Nota do resumo", [m.nota_media for m in historico], 1, 5, [1, 2, 3, 4, 5],
                         lambda v: fmt(v, 0), xs, dicas, "Sem notas do juiz nas execuções do período"),
    ]
    rotulos = [Tick(pos=x, rotulo=fmt_data(m.executado_em)) for x, m in zip(xs, historico)]
    return Tendencia(paineis=paineis, rotulos_x=rotulos, quantidade=n)


def grafico_tokens(
    base: Metricas, nova: Metricas, id_base: IdentidadeModelo, id_nova: IdentidadeModelo
) -> list[LinhaToken]:
    maximo = max(base.tokens_entrada + base.tokens_saida, nova.tokens_entrada + nova.tokens_saida)
    linhas = []
    for modelo, serie, m in ((id_base, "base", base), (id_nova, "nova", nova)):
        total = m.tokens_entrada + m.tokens_saida
        largura_entrada = m.tokens_entrada / maximo * 100
        largura_saida = m.tokens_saida / maximo * 100
        dica = Dica(titulo=f"{modelo.nome} · {modelo.prompt_versao}", linhas=[
            LinhaDica(rotulo="Entrada", valor=fmt(m.tokens_entrada, 0), serie=serie, provedor=modelo.provedor.chave),
            LinhaDica(rotulo="Saída", valor=fmt(m.tokens_saida, 0), serie=serie, provedor=modelo.provedor.chave),
            LinhaDica(rotulo="Total", valor=fmt(total, 0)),
        ])
        linhas.append(LinhaToken(
            modelo=modelo, serie=serie, total_fmt=fmt(total, 0),
            segmentos=[
                SegmentoToken(x=0, largura=largura_entrada, tipo="entrada", dica=dica),
                SegmentoToken(x=largura_entrada, largura=largura_saida, tipo="saida", dica=dica),
            ],
        ))
    return linhas


class LinhaMudanca(BaseModel):
    id_caso: str
    mensagem: str
    esperada: str
    antes: str | None
    depois: str | None
    resumo: str | None


class LinhaCaso(BaseModel):
    id_caso: str
    mensagem: str
    esperada: str
    obtida: str | None
    acertou: bool
    formato_valido: bool
    nota: int | None
    latencia_fmt: str
    custo_fmt: str
    resumo: str | None
    justificativa: str | None
    erro: str | None


def linhas_mudanca(mudancas: list[MudancaCaso], mensagens: dict[str, str], nova: dict[str, ResultadoCaso]) -> list[LinhaMudanca]:
    return [
        LinhaMudanca(
            id_caso=m.id_caso,
            mensagem=mensagens.get(m.id_caso, "—"),
            esperada=m.categoria_esperada,
            antes=m.obtido_antes,
            depois=m.obtido_depois,
            resumo=nova[m.id_caso].resumo_obtido if m.id_caso in nova else None,
        )
        for m in mudancas
    ]


def linhas_casos(nova: ExecucaoAvaliacao, mensagens: dict[str, str]) -> list[LinhaCaso]:
    return [
        LinhaCaso(
            id_caso=r.id_caso,
            mensagem=mensagens.get(r.id_caso, "—"),
            esperada=r.categoria_esperada,
            obtida=r.categoria_obtida,
            acertou=r.acertou,
            formato_valido=r.formato_valido,
            nota=r.nota_resumo,
            latencia_fmt=f"{fmt(r.latencia_ms, 0)} ms",
            custo_fmt=fmt(r.custo_usd, 6) if r.custo_usd is not None else "—",
            resumo=r.resumo_obtido,
            justificativa=r.justificativa_juiz,
            erro=r.erro,
        )
        for r in nova.resultados
    ]


def rotulo_execucao(e: ExecucaoAvaliacao) -> str:
    return f"{e.modelo} · {e.prompt_versao}"


def montar_contexto(base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao, limiares: Limiares) -> dict:
    comparacao = comparar(base, nova)
    veredito = avaliar_limiares(comparacao, limiares)
    dataset = carregar_dataset(str(RAIZ / "dataset" / f"golden_{nova.dataset_versao}.json"))
    mensagens = {caso.id: caso.mensagem for caso in dataset.casos}
    resultados_nova = {r.id_caso: r for r in nova.resultados}

    m_base, m_nova = calcular_metricas(base), calcular_metricas(nova)
    historico = [calcular_metricas(e) for e in carregar_historico(nova)]
    id_base, id_nova = identidades(base, nova)

    return {
        "modelo_base": id_base,
        "modelo_nova": id_nova,
        "provedores": [*PROVEDORES.values(), PROVEDOR_GENERICO],
        "base": base,
        "nova": nova,
        "metricas_base": m_base,
        "metricas_nova": m_nova,
        "rotulo_base": rotulo_execucao(base),
        "rotulo_nova": rotulo_execucao(nova),
        "periodo": f"{fmt_data(base.executado_em)} → {fmt_data(nova.executado_em)}",
        "veredito": veredito,
        "motivos": separar_motivos(veredito.motivos),
        "kpis": montar_kpis(m_base, m_nova),
        "comparacao": comparacao,
        "grafico_categorias": grafico_categorias(comparacao, nova, id_base, id_nova),
        "tendencia": grafico_tendencia(historico, id_nova),
        "visoes_latencia": grafico_latencia(base, nova, m_base, m_nova, id_base, id_nova),
        "tokens": grafico_tokens(m_base, m_nova, id_base, id_nova),
        "regressoes": linhas_mudanca(comparacao.regressoes, mensagens, resultados_nova),
        "melhorias": linhas_mudanca(comparacao.melhorias, mensagens, resultados_nova),
        "casos": linhas_casos(nova, mensagens),
        "acertos_nova": sum(r.acertou for r in nova.resultados),
        "dataset": dataset,
        "limiares": limiares,
        "custo_juiz_fmt": fmt(m_nova.custo_juiz, 5) if m_nova.custo_juiz is not None else None,
        "gerado_em": f"{datetime.now():%d/%m/%Y %H:%M}",
        "data_base": fmt_data(base.executado_em),
        "data_nova": fmt_data(nova.executado_em),
    }


def criar_ambiente() -> Environment:
    ambiente = Environment(
        loader=FileSystemLoader(RAIZ / "templates"),
        autoescape=select_autoescape(["html", "j2"], default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ambiente.filters["fmt"] = fmt
    return ambiente


def gerar_relatorio(caminho_base: str, caminho_nova: str) -> Path:
    base, nova = carregar_execucao(caminho_base), carregar_execucao(caminho_nova)
    limiares = carregar_limiares(str(RAIZ / "limiares.yaml"))
    contexto = montar_contexto(base, nova, limiares)
    html = criar_ambiente().get_template("relatorio.html.j2").render(**contexto)

    pasta = RAIZ / "relatorios"
    pasta.mkdir(exist_ok=True)
    nome = f"{datetime.now():%Y%m%d_%H%M%S}_{nova.prompt_versao}_{nova.modelo.replace('/', '_')}.html"
    caminho = pasta / nome
    caminho.write_text(html, encoding="utf-8")
    return caminho


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Uso: python relatorio.py <execucao_base.json> <execucao_nova.json>")
        sys.exit(1)

    print(gerar_relatorio(sys.argv[1], sys.argv[2]))
