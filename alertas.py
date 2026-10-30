import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx2 as httpx
from dotenv import load_dotenv

from contratos import Comparacao, MudancaCaso, Status, Veredito, VereditoDrift

load_dotenv()
logging.getLogger("httpx2").setLevel(logging.WARNING)

TIMEOUT_S = 10
MAX_REGRESSOES = 10
LIMITE_TITULO = 256
LIMITE_DESCRICAO = 4096
LIMITE_CAMPO = 1024
LIMITE_TOTAL = 6000
RODAPE = "Senderix Eval"

TITULOS: dict[Status, str] = {
    "aviso": "⚠️ AVISO: regressão detectada",
    "critico": "🚨 CRÍTICO: regressão detectada",
}
CORES: dict[Status, int] = {
    "aviso": 0xFBBF24,
    "critico": 0xF43F5E,
}


def cortar(texto: str, limite: int) -> str:
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def fmt(valor: float, casas: int) -> str:
    return f"{valor:.{casas}f}".replace(".", ",")


def fmt_variacao_pp(variacao: float) -> str:
    sinal = "−" if variacao < 0 else "+"
    return f"{sinal}{fmt(abs(variacao), 1)} pp"


def linha_taxa(c: Comparacao) -> str:
    return f"{fmt(c.taxa_base, 1)}% → {fmt(c.taxa_nova, 1)}% ({fmt_variacao_pp(c.taxa_nova - c.taxa_base)})"


def linha_regressao(m: MudancaCaso) -> str:
    antes = m.obtido_antes or "sem resposta"
    agora = m.obtido_depois or "sem resposta"
    return f"{m.id_caso}: esperado {m.categoria_esperada} | antes {antes} → agora {agora}"


def texto_regressoes(regressoes: list[MudancaCaso]) -> str:
    visiveis = [linha_regressao(m) for m in regressoes[:MAX_REGRESSOES]]
    while True:
        ocultas = len(regressoes) - len(visiveis)
        texto = "\n".join(visiveis + ([f"… e mais {ocultas}"] if ocultas else []))
        if len(texto) <= LIMITE_CAMPO or not visiveis:
            return texto
        visiveis.pop()


def campo(nome: str, valor: str) -> dict[str, Any]:
    return {"name": nome, "value": cortar(valor, LIMITE_CAMPO), "inline": False}


def montar_campos(c: Comparacao, veredito: Veredito, caminho_relatorio: Path | None) -> list[dict[str, Any]]:
    campos = [campo("Taxa de acerto", linha_taxa(c))]
    if c.nota_resumo_base is not None and c.nota_resumo_nova is not None:
        campos.append(campo("Nota do resumo", f"{fmt(c.nota_resumo_base, 2)} → {fmt(c.nota_resumo_nova, 2)}"))
    if veredito.motivos:
        campos.append(campo("Motivos", "\n".join(veredito.motivos)))
    if c.regressoes:
        campos.append(campo("Regressões", texto_regressoes(c.regressoes)))
    if caminho_relatorio is not None:
        campos.append(campo("Relatório", str(caminho_relatorio)))
    return campos


def tamanho_embed(embed: dict[str, Any]) -> int:
    return (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
    )


def ajustar_ao_limite_total(embed: dict[str, Any]) -> dict[str, Any]:
    for f in reversed(embed["fields"]):
        excesso = tamanho_embed(embed) - LIMITE_TOTAL
        if excesso <= 0:
            break
        f["value"] = cortar(f["value"], max(1, len(f["value"]) - excesso))
    excesso = tamanho_embed(embed) - LIMITE_TOTAL
    if excesso > 0:
        embed["description"] = cortar(embed["description"], max(1, len(embed["description"]) - excesso))
    return embed


def montar_embed(
    comparacao: Comparacao,
    veredito: Veredito,
    caminho_relatorio: Path | None = None,
    agora: datetime | None = None,
) -> dict[str, Any]:
    status = veredito.status
    if status == "ok":
        raise ValueError("Não há alerta para um veredito ok.")
    embed = {
        "title": cortar(TITULOS[status], LIMITE_TITULO),
        "description": cortar(
            f"**Base:** {comparacao.execucao_base}\n**Nova:** {comparacao.execucao_nova}", LIMITE_DESCRICAO
        ),
        "color": CORES[status],
        "fields": montar_campos(comparacao, veredito, caminho_relatorio),
        "footer": {"text": RODAPE},
        "timestamp": (agora or datetime.now(timezone.utc)).isoformat(),
    }
    return ajustar_ao_limite_total(embed)


def postar(url: str, payload: dict[str, Any]) -> bool:
    try:
        resposta = httpx.post(url, json=payload, timeout=TIMEOUT_S)
    except Exception as erro:
        print(f"Aviso: falha ao enviar alerta ao Discord ({type(erro).__name__}).")
        return False
    if not resposta.is_success:
        print(f"Aviso: o Discord recusou o alerta (HTTP {resposta.status_code}).")
        return False
    return True


def enviar_alerta(comparacao: Comparacao, veredito: Veredito, caminho_relatorio: Path | None = None) -> bool:
    if veredito.status == "ok":
        return False
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        print("Aviso: DISCORD_WEBHOOK_URL não definida; alerta não enviado.")
        return False
    return postar(url, {"embeds": [montar_embed(comparacao, veredito, caminho_relatorio)]})


TITULO_DRIFT = "📉 Drift lento detectado"
BLOCOS_SPARKLINE = "▁▂▃▄▅▆▇█"


def sparkline(valores: list[float]) -> str:
    if not valores:
        return ""
    menor, maior = min(valores), max(valores)
    if maior == menor:
        return BLOCOS_SPARKLINE[len(BLOCOS_SPARKLINE) // 2] * len(valores)
    escala = (len(BLOCOS_SPARKLINE) - 1) / (maior - menor)
    return "".join(BLOCOS_SPARKLINE[round((v - menor) * escala)] for v in valores)


def linha_serie(valores: list[float]) -> str:
    if not valores:
        return "sem execuções"
    return f"{sparkline(valores)}  {fmt(valores[0], 1)}% → {fmt(valores[-1], 1)}% (mín. {fmt(min(valores), 1)}%, máx. {fmt(max(valores), 1)}%)"


def linha_media_taxa(v: VereditoDrift) -> str:
    if v.taxa_referencia is None or v.taxa_recente is None or v.variacao_taxa_pp is None:
        return "sem dados"
    return f"{fmt(v.taxa_referencia, 1)}% → {fmt(v.taxa_recente, 1)}% ({fmt_variacao_pp(v.variacao_taxa_pp)})"


def linha_media_nota(v: VereditoDrift) -> str | None:
    if v.nota_referencia is None or v.nota_recente is None or v.variacao_nota is None:
        return None
    sinal = "−" if v.variacao_nota < 0 else "+"
    return f"{fmt(v.nota_referencia, 2)} → {fmt(v.nota_recente, 2)} ({sinal}{fmt(abs(v.variacao_nota), 2)})"


def montar_embed_drift(veredito: VereditoDrift, agora: datetime | None = None) -> dict[str, Any]:
    if veredito.status != "aviso":
        raise ValueError("Só há alerta de drift para o status aviso.")
    medias = f"médias das {veredito.janela} primeiras → {veredito.janela} últimas execuções"
    campos = [campo(f"Taxa de acerto ({medias})", linha_media_taxa(veredito))]
    nota = linha_media_nota(veredito)
    if nota is not None:
        campos.append(campo("Nota do resumo", nota))
    campos.append(campo("Série da taxa de acerto", linha_serie(veredito.serie_taxas)))
    if veredito.motivos:
        campos.append(campo("Motivos", "\n".join(veredito.motivos)))
    embed = {
        "title": cortar(TITULO_DRIFT, LIMITE_TITULO),
        "description": cortar(
            f"**Modelo:** {veredito.modelo}\n**Prompt:** {veredito.prompt_versao} · "
            f"**Dataset:** {veredito.dataset_versao}\n**Execuções:** {veredito.execucoes}",
            LIMITE_DESCRICAO,
        ),
        "color": CORES["aviso"],
        "fields": campos,
        "footer": {"text": RODAPE},
        "timestamp": (agora or datetime.now(timezone.utc)).isoformat(),
    }
    return ajustar_ao_limite_total(embed)


def enviar_alerta_drift(veredito: VereditoDrift) -> bool:
    if veredito.status != "aviso":
        return False
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        print("Aviso: DISCORD_WEBHOOK_URL não definida; alerta de drift não enviado.")
        return False
    return postar(url, {"embeds": [montar_embed_drift(veredito)]})
