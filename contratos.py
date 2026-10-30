from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Categoria = Literal["interessado", "duvida", "opt_out", "reclamacao", "fora_de_contexto"]

class Classificacao(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categoria: Categoria
    resumo: str = Field(min_length=1, max_length=200)


class PromptConfig(BaseModel):
    versao: str
    system_prompt: str
    modelo: str = "openai/gpt-40-mini"
    temperatura: float = Field(default=0, ge=0, le=2)

class CasoTeste(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mensagem: str
    categoria_esperada: Categoria
    resumo_ideal: str
    dificuldade: Literal["facil", "media", "dificil"]
    notas: str=""

class GoldenDataset(BaseModel):
    versao: str
    criado_em: str
    descricao: str
    casos: list[CasoTeste]

class ResultadoCaso(BaseModel):
    id_caso: str
    categoria_esperada: Categoria
    categoria_obtida: Categoria | None
    resumo_obtido: str | None
    formato_valido: bool
    acertou: bool
    latencia_ms: float
    tokens_entrada: int
    tokens_saida: int
    custo_usd: float | None
    erro: str | None = None
    nota_resumo: int | None = None
    justificativa_juiz: str | None = None
    custo_juiz_usd: float | None = None

class MetadadosChamada(BaseModel):
    latencia_ms: float
    tokens_entrada: int
    tokens_saida: int
    custo_usd: float | None

class ExecucaoAvaliacao(BaseModel):
    dataset_versao: str
    prompt_versao: str
    modelo: str
    executado_em: str
    resultados: list[ResultadoCaso]

class AvaliacaoJuiz(BaseModel):
    justificativa: str
    nota: int = Field(ge=1, le=5)

class MudancaCaso(BaseModel):
    id_caso: str
    categoria_esperada: Categoria
    obtido_antes: Categoria | None
    obtido_depois: Categoria | None


class Comparacao(BaseModel):
    execucao_base: str
    execucao_nova: str
    taxa_base: float
    taxa_nova: float
    acuracia_por_categoria: dict[str, tuple[float, float]]
    regressoes: list[MudancaCaso]
    melhorias: list[MudancaCaso]
    nota_resumo_base: float | None
    nota_resumo_nova: float | None

Status = Literal["ok","aviso","critico"]

class LimiaresDrift(BaseModel):
    janela: int = Field(ge=2)
    min_execucoes: int
    queda_taxa_pp: float = Field(gt=0)
    queda_nota_resumo: float = Field(gt=0)

    @model_validator(mode="after")
    def validar_min_execucoes(self) -> "LimiaresDrift":
        if self.min_execucoes <= self.janela:
            raise ValueError("min_execucoes deve ser maior que janela")
        return self

class Limiares(BaseModel):
    queda_aviso_pp: float = Field(gt=0)
    queda_critico_pp: float = Field(gt=0)
    queda_nota_resumo_aviso: float = Field(gt=0)
    categorias_criticas: list[Categoria] = []
    drift: LimiaresDrift | None = None

class Veredito(BaseModel):
    status: Status
    motivos: list[str]

StatusDrift = Literal["ok", "aviso", "historico_insuficiente"]

class VereditoDrift(BaseModel):
    status: StatusDrift
    modelo: str
    prompt_versao: str
    dataset_versao: str
    janela: int
    execucoes: int
    taxa_referencia: float | None = None
    taxa_recente: float | None = None
    variacao_taxa_pp: float | None = None
    nota_referencia: float | None = None
    nota_recente: float | None = None
    variacao_nota: float | None = None
    motivos: list[str]
    serie_taxas: list[float]
