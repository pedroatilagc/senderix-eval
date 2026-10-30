import statistics
from datetime import datetime
from pathlib import Path
from juiz import avaliar_resumo, carregar_config_juiz

from pydantic import ValidationError

from classificador import carregar_config, chamar_modelo, extrair_json
from contratos import (
    Classificacao,
    ExecucaoAvaliacao,
    GoldenDataset,
    PromptConfig,
    ResultadoCaso,
)


def carregar_dataset(caminho: str) -> GoldenDataset:
    texto = Path(caminho).read_text(encoding="utf-8")
    return GoldenDataset.model_validate_json(texto)


def executar_avaliacao(
    dataset: GoldenDataset, config: PromptConfig, config_juiz: PromptConfig
) -> list[ResultadoCaso]:
    resultados = []

    for caso in dataset.casos:
        conteudo, metadados = chamar_modelo(caso.mensagem, config)

        try:
            saida = Classificacao.model_validate_json(extrair_json(conteudo))
        except ValidationError as erro:
            resultados.append(
                ResultadoCaso(
                    id_caso=caso.id,
                    categoria_esperada=caso.categoria_esperada,
                    categoria_obtida=None,
                    resumo_obtido=None,
                    formato_valido=False,
                    acertou=False,
                    erro=str(erro),
                    **metadados.model_dump(),
                )
            )
            continue

        try:
            avaliacao, meta_juiz = avaliar_resumo(caso, saida.resumo, config_juiz)
            nota, justificativa, custo_juiz = avaliacao.nota, avaliacao.justificativa, meta_juiz.custo_usd
        except ValidationError:
            nota, justificativa, custo_juiz = None, None, None

        resultados.append(
            ResultadoCaso(
                id_caso=caso.id,
                categoria_esperada=caso.categoria_esperada,
                categoria_obtida=saida.categoria,
                resumo_obtido=saida.resumo,
                formato_valido=True,
                acertou=saida.categoria == caso.categoria_esperada,
                nota_resumo=nota,
                justificativa_juiz=justificativa,
                custo_juiz_usd=custo_juiz,
                **metadados.model_dump(),
            )
        )

    return resultados


def resumir(resultados: list[ResultadoCaso]) -> None:
    total = len(resultados)
    validos = [r for r in resultados if r.formato_valido]
    acertos = sum(r.acertou for r in validos)
    latencias = [r.latencia_ms for r in resultados]
    custos = [r.custo_usd for r in resultados if r.custo_usd is not None]

    print(f"Formato válido: {len(validos)}/{total} ({len(validos) / total * 100:.1f}%)")
    if validos:
        print(f"Acurácia (entre válidos): {acertos}/{len(validos)} ({acertos / len(validos) * 100:.1f}%)")
    print(f"Latência P50: {statistics.median(latencias):.0f} ms | P95: {statistics.quantiles(latencias, n=20)[18]:.0f} ms")
    print(f"Tokens: entrada {sum(r.tokens_entrada for r in resultados)} | saída {sum(r.tokens_saida for r in resultados)}")
    notas = [r.nota_resumo for r in resultados if r.nota_resumo is not None]
    if notas:
        print(f"Qualidade do resumo: média {statistics.mean(notas):.2f}/5 ({len(notas)} avaliados)")
    custos_juiz = [r.custo_juiz_usd for r in resultados if r.custo_juiz_usd is not None]
    if custos_juiz:
        print(f"Custo do juiz: US$ {sum(custos_juiz):.6f}")
    if custos:
        print(f"Custo total: US$ {sum(custos):.6f}")


def salvar_execucao(dataset: GoldenDataset, config: PromptConfig, resultados: list[ResultadoCaso]) -> Path:
    agora = datetime.now()
    execucao = ExecucaoAvaliacao(
        dataset_versao=dataset.versao,
        prompt_versao=config.versao,
        modelo=config.modelo,
        executado_em=agora.isoformat(timespec="seconds"),
        resultados=resultados,
    )

    pasta = Path("resultados")
    pasta.mkdir(exist_ok=True)
    nome = f"{agora:%Y%m%d_%H%M%S}_{config.versao}_{config.modelo.replace('/', '_')}.json"
    caminho = pasta / nome
    caminho.write_text(execucao.model_dump_json(indent=2), encoding="utf-8")
    return caminho


if __name__ == "__main__":
    dataset = carregar_dataset("dataset/golden_v2.json")
    print(f"Dataset {dataset.versao}: {len(dataset.casos)} casos\n")

    config_juiz = carregar_config_juiz()

    for modelo in [
        "openai/gpt-4o-mini",
        "anthropic/claude-haiku-4.5",
        "google/gemini-2.5-flash",
        "deepseek/deepseek-v4-flash",
    ]:
        config = carregar_config("v5", modelo=modelo)
        resultados = executar_avaliacao(dataset, config, config_juiz)

        print(f"=== prompt {config.versao} | {config.modelo} ===")
        resumir(resultados)
        for r in resultados:
            if not r.acertou:
                print(f"  ✗ {r.id_caso}: esperado {r.categoria_esperada}, obtido {r.categoria_obtida}")
        print(f"Salvo em: {salvar_execucao(dataset, config, resultados)}\n")