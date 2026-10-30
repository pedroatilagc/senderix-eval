"""Ponto de entrada do CI: avalia a configuração de produção contra a baseline aprovada.

Códigos de saída: 0 = ok/aviso, 1 = uso incorreto (config/baseline), 2 = crítico, 3 = erro de execução/infra.
"""
import argparse
import os
import shutil
import sys
import traceback
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from comparador import avaliar_limiares, carregar_execucao, carregar_limiares, comparar, imprimir
from contratos import Comparacao, ConfigProducao, ExecucaoAvaliacao, MudancaCaso, Veredito

RAIZ = Path(__file__).parent
CAMINHO_CONFIG = RAIZ / "config_producao.yaml"
CAMINHO_BASELINE = RAIZ / "baseline" / "execucao.json"
PASTA_SAIDA = RAIZ / "saida_ci"

SAIDA_OK, SAIDA_USO, SAIDA_CRITICO, SAIDA_ERRO = 0, 1, 2, 3

TITULOS = {
    "ok": "✅ Senderix Eval: OK",
    "aviso": "⚠️ Senderix Eval: AVISO",
    "critico": "🚨 Senderix Eval: CRÍTICO",
}


class ErroUso(Exception):
    """Configuração ou baseline inválida: corrigível por quem abriu o PR."""


class ErroInfra(Exception):
    """API fora do ar, chave inválida, limite de requisições etc."""


def carregar_config_producao(caminho: Path = CAMINHO_CONFIG) -> ConfigProducao:
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            return ConfigProducao.model_validate(yaml.safe_load(arquivo))
    except FileNotFoundError:
        raise ErroUso(f"{caminho.name} não encontrado.") from None
    except (yaml.YAMLError, ValidationError) as erro:
        raise ErroUso(f"{caminho.name} inválido: {erro}") from None


def carregar_baseline(config: ConfigProducao) -> tuple[Path, ExecucaoAvaliacao]:
    if not CAMINHO_BASELINE.exists():
        raise ErroUso(
            "Baseline não encontrada em baseline/execucao.json. "
            "Promova uma execução aprovada com: python promover_baseline.py <execucao.json>"
        )
    try:
        base = carregar_execucao(str(CAMINHO_BASELINE))
    except ValidationError as erro:
        raise ErroUso(f"baseline/execucao.json não é uma ExecucaoAvaliacao válida: {erro}") from None
    if base.dataset_versao != config.dataset:
        raise ErroUso(
            f"A baseline é do dataset {base.dataset_versao}, mas a produção usa o {config.dataset}. "
            "Gere uma execução no dataset novo (python eval_ci.py --apenas-executar) "
            "e promova-a com promover_baseline.py."
        )
    return CAMINHO_BASELINE, base


def sem_segredos(texto: str) -> str:
    for nome in ("OPENROUTER_API_KEY", "DISCORD_WEBHOOK_URL"):
        valor = os.getenv(nome)
        if valor:
            texto = texto.replace(valor, "***")
    return texto


def executar(config: ConfigProducao) -> Path:
    if not os.getenv("OPENROUTER_API_KEY"):
        raise ErroInfra("OPENROUTER_API_KEY não definida.")

    import openai

    try:
        # Import tardio: o classificador cria o cliente da API ao ser importado.
        from classificador import carregar_config
        from executor import carregar_dataset, executar_avaliacao, resumir, salvar_execucao
        from juiz import carregar_config_juiz

        dataset = carregar_dataset(f"dataset/golden_{config.dataset}.json")
        prompt = carregar_config(config.prompt, modelo=config.modelo)
        print(f"Avaliando prompt {prompt.versao} | {prompt.modelo} | dataset {dataset.versao} ({len(dataset.casos)} casos)...")
        resultados = executar_avaliacao(dataset, prompt, carregar_config_juiz())
    except FileNotFoundError as erro:
        raise ErroUso(f"Arquivo da configuração de produção não encontrado: {erro.filename}") from None
    except openai.APIStatusError as erro:
        raise ErroInfra(f"a API respondeu HTTP {erro.status_code} ({type(erro).__name__}): {erro.message}") from None
    except openai.OpenAIError as erro:
        raise ErroInfra(f"falha ao chamar a API ({type(erro).__name__}): {erro}") from None

    resumir(resultados)
    return salvar_execucao(dataset, prompt, resultados)


def fmt_variacao(delta: float, casas: int, sufixo: str = "") -> str:
    from relatorio import fmt

    sinal = "−" if delta < 0 else "+"
    return f"{sinal}{fmt(abs(delta), casas)}{sufixo}"


def tabela_metricas(base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao) -> list[str]:
    from relatorio import calcular_metricas, casas_custo, fmt

    mb, mn = calcular_metricas(base), calcular_metricas(nova)
    linhas = ["| Métrica | Base | Nova | Variação |", "|---|---:|---:|---:|"]

    def linha(rotulo: str, vb: float | None, vn: float | None, casas: int, prefixo: str = "", sufixo: str = "", sufixo_delta: str | None = None) -> None:
        celula = lambda v: "—" if v is None else f"{prefixo}{fmt(v, casas)}{sufixo}"
        delta = "—" if vb is None or vn is None else fmt_variacao(vn - vb, casas, sufixo if sufixo_delta is None else sufixo_delta)
        linhas.append(f"| {rotulo} | {celula(vb)} | {celula(vn)} | {delta} |")

    linha("Acurácia", mb.taxa_acerto, mn.taxa_acerto, 1, sufixo="%", sufixo_delta=" pp")
    linha("Formato válido", mb.conformidade_formato, mn.conformidade_formato, 1, sufixo="%", sufixo_delta=" pp")
    linha("Nota do resumo (1–5)", mb.nota_media, mn.nota_media, 2)
    linha("Latência P50", mb.latencia_p50, mn.latencia_p50, 0, sufixo=" ms")
    linha("Latência P95", mb.latencia_p95, mn.latencia_p95, 0, sufixo=" ms")
    custos = [c for c in (mb.custo_total, mn.custo_total) if c is not None]
    casas = casas_custo(min(custos)) if custos else 4
    linha("Custo do classificador", mb.custo_total, mn.custo_total, casas, prefixo="US$ ", sufixo_delta="")
    return linhas


def tabela_mudancas(mudancas: list[MudancaCaso]) -> list[str]:
    linhas = ["| Caso | Esperado | Antes | Agora |", "|---|---|---|---|"]
    for m in mudancas:
        linhas.append(
            f"| `{m.id_caso}` | {m.categoria_esperada} | {m.obtido_antes or 'sem resposta'} | {m.obtido_depois or 'sem resposta'} |"
        )
    return linhas


def montar_resumo(base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao, comparacao: Comparacao, veredito: Veredito) -> str:
    partes = [f"## {TITULOS[veredito.status]}", ""]
    partes += [
        f"**Configuração avaliada:** prompt `{nova.prompt_versao}` · modelo `{nova.modelo}` · dataset `{nova.dataset_versao}`  ",
        f"**Base:** {comparacao.execucao_base}  ",
        f"**Nova:** {comparacao.execucao_nova}",
        "",
    ]
    try:
        partes += tabela_metricas(base, nova)
    except Exception as erro:
        partes.append(f"_Não foi possível calcular a tabela de métricas ({type(erro).__name__})._")

    partes += ["", "### Motivos", ""]
    partes += [f"- {m}" for m in veredito.motivos] or ["Nenhum limiar ultrapassado."]

    partes += ["", f"### Regressões ({len(comparacao.regressoes)})", ""]
    partes += tabela_mudancas(comparacao.regressoes) if comparacao.regressoes else ["Nenhuma regressão."]

    if comparacao.melhorias:
        partes += ["", f"<details><summary>Melhorias ({len(comparacao.melhorias)})</summary>", ""]
        partes += tabela_mudancas(comparacao.melhorias)
        partes += ["", "</details>"]

    partes += ["", "<sub>Limiares em <code>limiares.yaml</code>. Relatório HTML e JSON da execução nos artefatos do workflow.</sub>"]
    return "\n".join(partes) + "\n"


def montar_resumo_erro(mensagem: str, codigo: int) -> str:
    return (
        "## ❌ Senderix Eval: não foi possível concluir\n\n"
        f"{mensagem}\n\n"
        f"<sub>Código de saída {codigo}. Detalhes no log do workflow.</sub>\n"
    )


def preparar_saida() -> None:
    PASTA_SAIDA.mkdir(exist_ok=True)
    for arquivo in PASTA_SAIDA.iterdir():
        if arquivo.is_file():
            arquivo.unlink()


def escrever_resumo(texto: str) -> None:
    caminho = PASTA_SAIDA / "resumo.md"
    caminho.write_text(sem_segredos(texto), encoding="utf-8")
    print(f"Resumo: {caminho.relative_to(RAIZ)}")


def gerar_artefatos(caminho_base: Path, caminho_nova: Path) -> Path | None:
    try:
        shutil.copy2(caminho_nova, PASTA_SAIDA / caminho_nova.name)
    except Exception as erro:
        print(f"Aviso: não foi possível copiar a execução para {PASTA_SAIDA.name}/ ({type(erro).__name__}).")
    try:
        from relatorio import gerar_relatorio

        caminho_relatorio = gerar_relatorio(str(caminho_base), str(caminho_nova))
        shutil.copy2(caminho_relatorio, PASTA_SAIDA / "relatorio.html")
        print(f"Relatório: {caminho_relatorio.relative_to(RAIZ)}")
        return caminho_relatorio.relative_to(RAIZ)
    except Exception as erro:
        print(f"Aviso: não foi possível gerar o relatório ({type(erro).__name__}: {sem_segredos(str(erro))}).")
        return None


def alertar(comparacao: Comparacao, veredito: Veredito, caminho_relatorio: Path | None) -> None:
    try:
        from alertas import enviar_alerta

        if enviar_alerta(comparacao, veredito, caminho_relatorio):
            print("Alerta enviado ao Discord.")
    except Exception as erro:
        print(f"Aviso: falha ao enviar alerta ({type(erro).__name__}).")


def ler_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Eval de CI: configuração de produção × baseline aprovada.")
    parser.add_argument("--alertar", action="store_true", help="envia o alerta ao Discord (só aviso/crítico)")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument(
        "--execucao-nova",
        type=Path,
        metavar="ARQUIVO",
        help="usa uma execução já salva como 'nova' em vez de rodar a avaliação (teste, sem custo de API)",
    )
    modo.add_argument(
        "--apenas-executar",
        action="store_true",
        help="só roda e salva a avaliação da configuração de produção (para promover uma baseline nova)",
    )
    return parser.parse_args()


def main() -> int:
    args = ler_argumentos()
    os.chdir(RAIZ)  # executor e classificador usam caminhos relativos (prompts/, dataset/, resultados/)
    load_dotenv()
    preparar_saida()

    try:
        config = carregar_config_producao()
        print(f"Produção: prompt {config.prompt} | {config.modelo} | dataset {config.dataset}")
        if args.apenas_executar:
            print(f"Execução salva em: {executar(config)}")
            return SAIDA_OK

        caminho_base, base = carregar_baseline(config)
        caminho_nova = args.execucao_nova or executar(config)
        nova = carregar_execucao(str(caminho_nova))
        comparacao = comparar(base, nova)
        veredito = avaliar_limiares(comparacao, carregar_limiares())
    except ErroInfra as erro:
        mensagem = f"Erro de infraestrutura: {sem_segredos(str(erro))}"
        print(mensagem, file=sys.stderr)
        escrever_resumo(montar_resumo_erro(mensagem, SAIDA_ERRO))
        return SAIDA_ERRO
    except (ErroUso, OSError, ValueError) as erro:  # ValueError inclui ValidationError e datasets diferentes
        mensagem = f"Uso incorreto: {sem_segredos(str(erro))}"
        print(mensagem, file=sys.stderr)
        escrever_resumo(montar_resumo_erro(mensagem, SAIDA_USO))
        return SAIDA_USO
    except Exception as erro:
        print(sem_segredos(traceback.format_exc()), file=sys.stderr)
        mensagem = f"Erro inesperado na execução ({type(erro).__name__})."
        escrever_resumo(montar_resumo_erro(mensagem, SAIDA_ERRO))
        return SAIDA_ERRO

    print()
    imprimir(comparacao)
    print(f"\nVeredito: {veredito.status.upper()}")
    for motivo in veredito.motivos:
        print(f"  - {motivo}")
    print()

    caminho_relatorio = gerar_artefatos(caminho_base, Path(caminho_nova))
    try:
        resumo = montar_resumo(base, nova, comparacao, veredito)
    except Exception as erro:
        print(f"Aviso: resumo completo indisponível ({type(erro).__name__}).")
        resumo = f"## {TITULOS[veredito.status]}\n\n" + "\n".join(f"- {m}" for m in veredito.motivos) + "\n"
    escrever_resumo(resumo)

    if args.alertar:
        alertar(comparacao, veredito, caminho_relatorio)

    return SAIDA_CRITICO if veredito.status == "critico" else SAIDA_OK


if __name__ == "__main__":
    sys.exit(main())
