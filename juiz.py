from classificador import carregar_config, chamar_modelo, extrair_json
from contratos import AvaliacaoJuiz, CasoTeste, MetadadosChamada, PromptConfig

MODELO_JUIZ = "openai/gpt-4.1"


def carregar_config_juiz() -> PromptConfig:
    return carregar_config("juiz_v1", modelo=MODELO_JUIZ)


def avaliar_resumo(
    caso: CasoTeste, resumo_obtido: str, config_juiz: PromptConfig
) -> tuple[AvaliacaoJuiz, MetadadosChamada]:
    entrada = (
        f"MENSAGEM: {caso.mensagem}\n"
        f"RESUMO DE REFERÊNCIA: {caso.resumo_ideal}\n"
        f"RESUMO AVALIADO: {resumo_obtido}"
    )
    conteudo, metadados = chamar_modelo(entrada, config_juiz)
    avaliacao = AvaliacaoJuiz.model_validate_json(extrair_json(conteudo))
    return avaliacao, metadados