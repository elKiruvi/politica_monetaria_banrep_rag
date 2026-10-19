# Imagen local del proyecto (no oficial). Sin PDF, índices ni secretos: los datos van en /datos.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencias primero (capa reutilizable), luego el código.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && rm /usr/local/bin/uv

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin banrep \
    && mkdir /datos && chown banrep:banrep /datos
USER banrep

ENV PATH=/app/.venv/bin:$PATH \
    BANREP_DATA_DIR=/datos \
    BANREP_OLLAMA_URL=http://host.docker.internal:11434 \
    BANREP_OLLAMA_LOCAL_HOSTS=host.docker.internal,ollama
VOLUME ["/datos"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3).status == 200 else 1)"]

# Dentro del contenedor se escucha en 0.0.0.0; Compose publica el puerto solo en 127.0.0.1 del
# equipo y la API sigue exigiendo un Host local (protección contra DNS rebinding).
CMD ["banrep-rag", "servir", "--host", "0.0.0.0", "--port", "8000", "--permitir-red"]
