"""Promove uma execução aprovada a baseline do CI (baseline/execucao.json).

A promoção é manual: rode este script e inclua a baseline nova no PR.
"""
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from comparador import taxa_acerto
from contratos import ConfigProducao, ExecucaoAvaliacao
from eval_ci import CAMINHO_BASELINE, ErroUso, carregar_config_producao


def divergencias(execucao: ExecucaoAvaliacao, config: ConfigProducao) -> list[str]:
    pares = [
        ("prompt", execucao.prompt_versao, config.prompt),
        ("modelo", execucao.modelo, config.modelo),
        ("dataset", execucao.dataset_versao, config.dataset),
    ]
    return [
        f"{campo}: a execução usa {obtido!r}, a produção usa {esperado!r}"
        for campo, obtido, esperado in pares
        if obtido != esperado
    ]


def recusar(mensagem: str) -> None:
    print(f"Promoção recusada: {mensagem}")
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python promover_baseline.py <execucao.json>")
        sys.exit(1)

    origem = Path(sys.argv[1])
    try:
        execucao = ExecucaoAvaliacao.model_validate_json(origem.read_text(encoding="utf-8"))
    except OSError as erro:
        recusar(f"não foi possível ler {origem} ({type(erro).__name__}).")
    except ValidationError as erro:
        recusar(f"{origem} não é uma ExecucaoAvaliacao válida.\n{erro}")
    if not execucao.resultados:
        recusar(f"{origem} não tem resultados.")

    try:
        config = carregar_config_producao()
    except ErroUso as erro:
        recusar(str(erro))

    problemas = divergencias(execucao, config)
    if problemas:
        recusar(
            "a configuração da execução não bate com config_producao.yaml:\n"
            + "\n".join(f"  - {p}" for p in problemas)
        )

    if origem.resolve() == CAMINHO_BASELINE.resolve():
        print("Essa execução já é a baseline.")
        sys.exit(0)

    CAMINHO_BASELINE.parent.mkdir(exist_ok=True)
    shutil.copyfile(origem, CAMINHO_BASELINE)
    print(f"Baseline promovida: {CAMINHO_BASELINE.relative_to(CAMINHO_BASELINE.parent.parent)}")
    print(f"  prompt {execucao.prompt_versao} | {execucao.modelo} | dataset {execucao.dataset_versao}")
    print(f"  executada em {execucao.executado_em} | acurácia {taxa_acerto(execucao.resultados):.1f}%")
    print("Inclua baseline/execucao.json no PR.")
