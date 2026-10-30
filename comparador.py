import statistics
import sys
from collections import defaultdict
from pathlib import Path
import yaml
from contratos import Comparacao, ExecucaoAvaliacao, Limiares, MudancaCaso, ResultadoCaso, Status, Veredito

def carregar_execucao(caminho: str) -> ExecucaoAvaliacao:
    texto = Path(caminho).read_text(encoding="utf-8")
    return ExecucaoAvaliacao.model_validate_json(texto)


def taxa_acerto(resultados: list[ResultadoCaso]) -> float:
    return sum(r.acertou for r in resultados) / len(resultados) * 100


def acuracia_por_categoria(resultados: list[ResultadoCaso]) -> dict[str, float]:
    grupos = defaultdict(list)
    for r in resultados:
        grupos[r.categoria_esperada].append(r.acertou)
    return {categoria: sum(acertos) / len(acertos) * 100 for categoria, acertos in grupos.items()}


def media_notas(resultados: list[ResultadoCaso]) -> float | None:
    notas = [r.nota_resumo for r in resultados if r.nota_resumo is not None]
    return statistics.mean(notas) if notas else None


def comparar(base: ExecucaoAvaliacao, nova: ExecucaoAvaliacao) -> Comparacao:
    if base.dataset_versao != nova.dataset_versao:
        raise ValueError(
            f"Datasets diferentes ({base.dataset_versao} vs {nova.dataset_versao}): a comparação não faz sentido."
        )

    antes = {r.id_caso: r for r in base.resultados}
    regressoes, melhorias = [], []

    for depois in nova.resultados:
        anterior = antes.get(depois.id_caso)
        if anterior is None:
            continue

        mudanca = MudancaCaso(
            id_caso=depois.id_caso,
            categoria_esperada=depois.categoria_esperada,
            obtido_antes=anterior.categoria_obtida,
            obtido_depois=depois.categoria_obtida,
        )
        if anterior.acertou and not depois.acertou:
            regressoes.append(mudanca)
        elif not anterior.acertou and depois.acertou:
            melhorias.append(mudanca)

    categorias_base = acuracia_por_categoria(base.resultados)
    categorias_nova = acuracia_por_categoria(nova.resultados)

    return Comparacao(
        execucao_base=f"{base.prompt_versao} | {base.modelo} | {base.executado_em}",
        execucao_nova=f"{nova.prompt_versao} | {nova.modelo} | {nova.executado_em}",
        taxa_base=taxa_acerto(base.resultados),
        taxa_nova=taxa_acerto(nova.resultados),
        acuracia_por_categoria={
            categoria: (categorias_base.get(categoria, 0.0), categorias_nova[categoria])
            for categoria in categorias_nova
        },
        regressoes=regressoes,
        melhorias=melhorias,
        nota_resumo_base=media_notas(base.resultados),
        nota_resumo_nova=media_notas(nova.resultados),
    )


def imprimir(c: Comparacao) -> None:
    print(f"Base: {c.execucao_base}")
    print(f"Nova: {c.execucao_nova}\n")

    print(f"Taxa de acerto: {c.taxa_base:.1f}% → {c.taxa_nova:.1f}% ({c.taxa_nova - c.taxa_base:+.1f} pp)")
    if c.nota_resumo_base is not None and c.nota_resumo_nova is not None:
        print(f"Nota do resumo: {c.nota_resumo_base:.2f} → {c.nota_resumo_nova:.2f}")

    print("\nAcurácia por categoria:")
    for categoria, (antes, depois) in sorted(c.acuracia_por_categoria.items()):
        marcador = "▼" if depois < antes else "▲" if depois > antes else " "
        print(f"  {marcador} {categoria:<18} {antes:5.1f}% → {depois:5.1f}%")

    print(f"\nRegressões ({len(c.regressoes)}):")
    for m in c.regressoes:
        print(f"  ✗ {m.id_caso}: esperado {m.categoria_esperada} | antes {m.obtido_antes} → agora {m.obtido_depois}")

    print(f"\nMelhorias ({len(c.melhorias)}):")
    for m in c.melhorias:
        print(f"  ✓ {m.id_caso}: esperado {m.categoria_esperada} | antes {m.obtido_antes} → agora {m.obtido_depois}")

NIVEL = {"ok": 0, "aviso": 1, "critico": 2}


def carregar_limiares(caminho: str = "limiares.yaml") -> Limiares:
    with open(caminho, encoding="utf-8") as arquivo:
        return Limiares.model_validate(yaml.safe_load(arquivo))


def avaliar_limiares(c: Comparacao, limiares: Limiares) -> Veredito:
    alertas: list[tuple[Status, str]] = []

    queda = c.taxa_base - c.taxa_nova
    if queda >= limiares.queda_critico_pp:
        alertas.append(("critico", f"taxa de acerto caiu {queda:.1f} pp"))
    elif queda >= limiares.queda_aviso_pp:
        alertas.append(("aviso", f"taxa de acerto caiu {queda:.1f} pp"))

    for m in c.regressoes:
        if m.categoria_esperada in limiares.categorias_criticas:
            alertas.append(("aviso", f"regressão em categoria crítica: {m.id_caso} ({m.categoria_esperada})"))

    if c.nota_resumo_base is not None and c.nota_resumo_nova is not None:
        queda_nota = c.nota_resumo_base - c.nota_resumo_nova
        if queda_nota >= limiares.queda_nota_resumo_aviso:
            alertas.append(("aviso", f"nota do resumo caiu {queda_nota:.2f}"))

    status = max((nivel for nivel, _ in alertas), key=NIVEL.get, default="ok")
    return Veredito(status=status, motivos=[f"[{nivel}] {motivo}" for nivel, motivo in alertas])


def alertar(caminho_base: str, caminho_nova: str, comparacao: Comparacao, veredito: Veredito) -> None:
    caminho_relatorio = None
    try:
        from relatorio import gerar_relatorio

        caminho_relatorio = gerar_relatorio(caminho_base, caminho_nova)
    except Exception as erro:
        print(f"Aviso: não foi possível gerar o relatório ({type(erro).__name__}: {erro}).")
    try:
        from alertas import enviar_alerta

        if enviar_alerta(comparacao, veredito, caminho_relatorio):
            print("Alerta enviado ao Discord.")
    except Exception as erro:
        print(f"Aviso: falha ao enviar alerta ({type(erro).__name__}).")


if __name__ == "__main__":
    argumentos = sys.argv[1:]
    com_alerta = "--alertar" in argumentos
    if com_alerta:
        argumentos.remove("--alertar")
    if len(argumentos) != 2:
        print("Uso: python comparador.py <execucao_base.json> <execucao_nova.json> [--alertar]")
        sys.exit(1)

    comparacao = comparar(carregar_execucao(argumentos[0]), carregar_execucao(argumentos[1]))
    imprimir(comparacao)

    veredito = avaliar_limiares(comparacao, carregar_limiares())
    print(f"\nVeredito: {veredito.status.upper()}")
    for motivo in veredito.motivos:
        print(f"  - {motivo}")

    if com_alerta:
        alertar(argumentos[0], argumentos[1], comparacao, veredito)

    sys.exit(2 if veredito.status == "critico" else 0)