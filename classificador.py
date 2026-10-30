import os
from dotenv import load_dotenv
from openai import OpenAI
import yaml
import time 

from contratos import Classificacao, PromptConfig, MetadadosChamada

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    max_retries=5,
)

def carregar_config(versao: str, modelo: str = "openai/gpt-4o-mini") -> PromptConfig:
    with open(f"prompts/{versao}.yaml", encoding="utf-8") as arquivo:
        dados = yaml.safe_load(arquivo)
    return PromptConfig(
        versao=dados["versao"],
        system_prompt=dados["system_prompt"],
        modelo=modelo,
    )

def extrair_json(texto: str) -> str:
    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio == -1 or fim == -1:
        return texto
    return texto[inicio : fim + 1]

def chamar_modelo(mensagem: str, config: PromptConfig) -> tuple[str, MetadadosChamada]:
    inicio = time.perf_counter()
    resposta = client.chat.completions.create(
        model=config.modelo,
        temperature=config.temperatura,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": config.system_prompt},
            {"role": "user", "content": mensagem},
        ],
        extra_body={"usage": {"include": True}},
    )
    latencia_ms = (time.perf_counter() - inicio) * 1000

    uso = resposta.usage
    metadados = MetadadosChamada(
        latencia_ms=latencia_ms,
        tokens_entrada=uso.prompt_tokens,
        tokens_saida=uso.completion_tokens,
        custo_usd=getattr(uso, "cost", None),
    )
    conteudo = resposta.choices[0].message.content or ""
    return conteudo, metadados


def classificar_mensagem(mensagem: str, config: PromptConfig) -> Classificacao:
    conteudo, _ = chamar_modelo(mensagem, config)
    return Classificacao.model_validate_json(extrair_json(conteudo))
    conteudo = resposta.choices[0].message.content
    return Classificacao.model_validate_json(extrair_json(conteudo))

if __name__ == "__main__":
    exemplos = [
        "Quanto custa o plano de 3 meses?",
        "para de me mandar isso, que saco",
        "Oi, tudo bem?",
        "Tenho interesse sim, pode me ligar amanhã",
        "comprei semana passada e até agora nada chegou",
    ]
    for versao in ["v1", "v2"]:
        config = carregar_config(versao)
        print(f"\n=== {config.versao} | {config.modelo} ===")
        for msg in exemplos:
            resultado = classificar_mensagem(msg, config)
            print(f"{msg!r} -> {resultado.categoria} | {resultado.resumo}")



