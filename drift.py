import argparse
import statistics
import sys
from pathlib import Path

from alertas import enviar_alerta_drift, fmt, linha_media_nota, linha_media_taxa, linha_serie
from comparador import carregar_execucao, carregar_limiares, media_notas, taxa_acerto
from contratos import ExecucaoAvaliacao, LimiaresDrift, VereditoDrift

RAIZ = Path(__file__).parent


def carregar_historico(pasta: Path, modelo: str, prompt: str, dataset: str) -> list[ExecucaoAvaliacao]:
    execucoes = [carregar_execucao(str(p)) for p in sorted(pasta.glob("*.json"))]
    mesmas = [
        e for e in execucoes
        if e.modelo == modelo and e.prompt_versao == prompt and e.dataset_versao == dataset
    ]
    return sorted(mesmas, key=lambda e: e.executado_em)


def medias_das_janelas(valores: list[float], janela: int) -> tuple[float, float]:
    return statistics.mean(valores[:janela]), statistics.mean(valores[-janela:])


def avaliar_drift(
    historico: list[ExecucaoAvaliacao],
    limiares: LimiaresDrift,
    modelo: str,
    prompt: str,
    dataset: str,
) -> VereditoDrift:
    serie = [taxa_acerto(e.resultados) for e in historico]
    veredito = VereditoDrift(
        status="historico_insuficiente",
        modelo=modelo,
        prompt_versao=prompt,
        dataset_versao=dataset,
        janela=limiares.janela,
        execucoes=len(historico),
        motivos=[],
        serie_taxas=serie,
    )
    if len(historico) < limiares.min_execucoes:
        veredito.motivos = [f"{len(historico)} execuções no histórico; o mínimo é {limiares.min_execucoes}"]
        return veredito

    motivos: list[str] = []
    taxa_ref, taxa_rec = medias_das_janelas(serie, limiares.janela)
    veredito.taxa_referencia, veredito.taxa_recente, veredito.variacao_taxa_pp = taxa_ref, taxa_rec, taxa_rec - taxa_ref
    if taxa_ref - taxa_rec >= limiares.queda_taxa_pp:
        motivos.append(f"[aviso] taxa de acerto média caiu {fmt(taxa_ref - taxa_rec, 1)} pp")

    notas = [n for n in (media_notas(e.resultados) for e in historico) if n is not None]
    if len(notas) >= limiares.min_execucoes:
        nota_ref, nota_rec = medias_das_janelas(notas, limiares.janela)
        veredito.nota_referencia, veredito.nota_recente, veredito.variacao_nota = nota_ref, nota_rec, nota_rec - nota_ref
        if nota_ref - nota_rec >= limiares.queda_nota_resumo:
            motivos.append(f"[aviso] nota média do resumo caiu {fmt(nota_ref - nota_rec, 2)}")

    veredito.status = "aviso" if motivos else "ok"
    veredito.motivos = motivos
    return veredito


def imprimir(v: VereditoDrift) -> None:
    print(f"Configuração: {v.modelo} | prompt {v.prompt_versao} | dataset {v.dataset_versao}")
    print(f"Execuções no histórico: {v.execucoes}\n")
    print(f"Série da taxa de acerto: {linha_serie(v.serie_taxas)}")
    if v.status != "historico_insuficiente":
        print(f"\nMédias das {v.janela} primeiras → {v.janela} últimas execuções:")
        print(f"  Taxa de acerto: {linha_media_taxa(v)}")
        nota = linha_media_nota(v)
        print(f"  Nota do resumo: {nota or 'sem execuções com nota suficientes'}")
    print(f"\nVeredito de drift: {v.status.upper()}")
    for motivo in v.motivos:
        print(f"  - {motivo}")


def ler_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detecta drift lento entre execuções de uma mesma configuração.")
    parser.add_argument("--modelo", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--pasta", type=Path, default=Path("resultados"))
    parser.add_argument("--alertar", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    # Drift indica tendência, não bloqueia PR: sai sempre com 0, inclusive em aviso.
    # Erro de uso sai com 2 (argparse); é aceitável porque este script não decide bloqueio.
    args = ler_argumentos()
    limiares = carregar_limiares(str(RAIZ / "limiares.yaml")).drift
    if limiares is None:
        print("limiares.yaml não tem a seção drift.")
        sys.exit(0)

    veredito = avaliar_drift(
        carregar_historico(args.pasta, args.modelo, args.prompt, args.dataset),
        limiares,
        args.modelo,
        args.prompt,
        args.dataset,
    )
    imprimir(veredito)

    if args.alertar:
        try:
            if enviar_alerta_drift(veredito):
                print("\nAlerta de drift enviado ao Discord.")
        except Exception as erro:
            print(f"Aviso: falha ao enviar alerta de drift ({type(erro).__name__}).")

    sys.exit(0)
