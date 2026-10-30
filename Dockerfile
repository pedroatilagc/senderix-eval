# syntax=docker/dockerfile:1

# Dependências num venv isolado: esta camada só é refeita quando requirements.txt muda.
FROM python:3.11-slim AS dependencias
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM python:3.11-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8
RUN useradd --create-home --uid 10001 senderix \
    && install -d -o senderix -g senderix /app
WORKDIR /app
COPY --from=dependencias /opt/venv /opt/venv
COPY --chown=senderix:senderix . .
USER senderix

# Segredos (OPENROUTER_API_KEY, DISCORD_WEBHOOK_URL) só em tempo de execução: docker run --env-file .env
ENTRYPOINT ["python", "eval_ci.py"]
