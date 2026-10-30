import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from comparador import carregar_execucao, carregar_limiares, taxa_acerto
from contratos import Categoria, ExecucaoAvaliacao, ResultadoCaso

RAIZ = Path(__file__).parent
PASTA_REAL = RAIZ / "resultados"
CENARIOS: dict[str, list[float]] = {
    "estavel": [97, 93, 96, 95, 93, 97, 94, 96, 93, 97],
    "queda_lenta": [97, 97, 94, 94, 90, 90, 87, 87, 83, 83],
}


def categoria_errada(esperada: Categoria) -> Categoria:
    return "fora_de_contexto" if esperada != "fora_de_contexto" else "duvida"


def acertar(r: ResultadoCaso) -> ResultadoCaso:
    return r.model_copy(
        update={"categoria_obtida": r.categoria_esperada, "acertou": True, "formato_valido": True, "erro": None}
    )


def errar(r: ResultadoCaso) -> ResultadoCaso:
    return r.model_copy(update={"categoria_obtida": categoria_errada(r.categoria_esperada), "acertou": False})


def com_acertos(base: ExecucaoAvaliacao, alvo: int, protegidas: list[Categoria]) -> list[ResultadoCaso]:
    atuais = sum(r.acertou for r in base.resultados)
    if alvo >= atuais:
        candidatos = [r.id_caso for r in base.resultados if not r.acertou][: alvo - atuais]
        falta, mudar = alvo - atuais, acertar
    else:
        candidatos = [
            r.id_caso for r in base.resultados if r.acertou and r.categoria_esperada not in protegidas
        ][: atuais - alvo]
        falta, mudar = atuais - alvo, errar
    if len(candidatos) < falta:
        raise ValueError(f"Não há casos suficientes para chegar a {alvo} acertos.")
    alterar = set(candidatos)
    return [mudar(r) if r.id_caso in alterar else r for r in base.resultados]


def simular(base: ExecucaoAvaliacao, taxas: list[float], protegidas: list[Categoria]) -> list[ExecucaoAvaliacao]:
    inicio = datetime.fromisoformat(base.executado_em) - timedelta(days=len(taxas) - 1)
    total = len(base.resultados)
    return [
        base.model_copy(
            update={
                "executado_em": (inicio + timedelta(days=i)).isoformat(timespec="seconds"),
                "resultados": com_acertos(base, round(taxa / 100 * total), protegidas),
            }
        )
        for i, taxa in enumerate(taxas)
    ]


def nome_arquivo(e: ExecucaoAvaliacao) -> str:
    data = datetime.fromisoformat(e.executado_em)
    return f"{data:%Y%m%d_%H%M%S}_{e.prompt_versao}_{e.modelo.replace('/', '_')}_simulado.json"


def salvar(execucoes: list[ExecucaoAvaliacao], pasta: Path) -> list[Path]:
    if pasta.resolve().is_relative_to(PASTA_REAL.resolve()):
        raise ValueError("A simulação nunca escreve em resultados/.")
    pasta.mkdir(parents=True, exist_ok=True)
    caminhos = []
    for e in execucoes:
        caminho = pasta / nome_arquivo(e)
        caminho.write_text(e.model_dump_json(indent=2), encoding="utf-8")
        caminhos.append(caminho)
    return caminhos


def ler_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera históricos sintéticos a partir de uma execução real.")
    parser.add_argument("execucao", type=Path, help="execução real de resultados/ usada como ponto de partida")
    parser.add_argument("--destino", type=Path, default=RAIZ / "resultados_simulados")
    return parser.parse_args()


if __name__ == "__main__":
    args = ler_argumentos()
    base = carregar_execucao(str(args.execucao))
    protegidas = carregar_limiares(str(RAIZ / "limiares.yaml")).categorias_criticas
    try:
        for cenario, taxas in CENARIOS.items():
            execucoes = simular(base, taxas, protegidas)
            salvar(execucoes, args.destino / cenario)
            serie = " ".join(f"{taxa_acerto(e.resultados):.1f}" for e in execucoes)
            print(f"{cenario}: {len(execucoes)} execuções em {args.destino / cenario} | taxas {serie}")
    except ValueError as erro:
        print(f"Erro: {erro}")
        sys.exit(1)
