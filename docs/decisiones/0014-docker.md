# ADR 0014 — Docker y Docker Compose

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F8B, issue #9).

## Decisión

- **Docker es opcional:** `uv` sigue siendo la forma principal de instalar y usar el proyecto.
- **Imagen** (`Dockerfile`):
  - Base `python:3.12-slim-bookworm` y `uv` 0.12.9 copiado de su imagen oficial, que se elimina tras instalar.
  - Dependencias con `uv sync --frozen --no-dev`, desde el mismo `uv.lock`.
  - Usuario sin privilegios (UID 10001) y `HEALTHCHECK` a `/api/v1/health`.
  - **No contiene PDF, índices, datos ni secretos:** `.dockerignore` excluye `data/`, `*.pdf`, `.env`, `tests/`, `docs/` y `.git/`. Los datos van en el volumen `/datos` (`BANREP_DATA_DIR`).
- **Red y seguridad:**
  - Dentro del contenedor el servicio escucha en `0.0.0.0` con `--permitir-red`. Compose publica el puerto **solo en `127.0.0.1`** del equipo.
  - Aun con `--permitir-red`, la API sigue exigiendo un `Host` local (protección contra DNS rebinding). Se pueden añadir nombres con `BANREP_ALLOWED_HOSTS`; «todos» solo con un `*` explícito. Esto corrige lo descrito en la ADR 0011, donde `--permitir-red` aceptaba cualquier `Host`.
  - El contenedor corre con sistema de archivos de solo lectura (más `tmpfs` en `/tmp`), `no-new-privileges`, sin capacidades (`cap_drop: ALL`) y con límites de 4 GB de memoria y 2 CPU. El procesamiento de un PDF ya se limita a 3 GB en su proceso aislado.
- **Ollama:**
  - Por defecto, el del equipo (`host.docker.internal`, con `host-gateway`).
  - Perfiles `ollama` (CPU) y `ollama-gpu` (NVIDIA, requiere NVIDIA Container Toolkit) con `ollama/ollama:0.33.2`, volumen propio y puerto solo local.
  - `BANREP_OLLAMA_LOCAL_HOSTS` declara esos nombres como el mismo equipo, para no emitir avisos de privacidad falsos.
- **Permisos del catálogo:** el servicio corre con `BANREP_UID` y `BANREP_GID` (por defecto 10001). Para escribir en un catálogo existente del equipo: `BANREP_UID=$(id -u) BANREP_GID=$(id -g) docker compose up -d`.

## Uso

```sh
docker compose build
BANREP_UID=$(id -u) BANREP_GID=$(id -g) docker compose up -d       # web y API en http://127.0.0.1:8000
docker compose run --rm banrep-rag banrep-rag importar /datos/entrada/informe.pdf --activar
docker compose run --rm banrep-rag banrep-rag actualizar --continuar-si-falla
BANREP_OLLAMA_URL=http://ollama:11434 docker compose --profile ollama up -d   # Ollama en contenedor
docker compose exec ollama ollama pull qwen3:4b-instruct                     # una vez, ~2,5 GB
```

## Verificación

- En el equipo de desarrollo **no hay Docker** y no se instaló, porque sería un cambio global no autorizado.
- La verificación real corre en GitHub Actions (`.github/workflows/docker.yml`) en cada PR que toca la imagen y en `main`:
  - `docker compose config` y la construcción de la imagen.
  - Que la imagen no contenga PDF, `.sqlite` ni `.npy`, y que use un usuario distinto de root.
  - `docker compose up` sobre un catálogo vacío hasta el estado *healthy*.
  - `/api/v1/health` 200, `/api/v1/ready` 503 (sin índice no está lista), `/` 200 y un `Host` ajeno 400.

## Límites

- La prueba en GitHub Actions no incluye Ollama ni GPU: los perfiles `ollama` y `ollama-gpu` están validados por `docker compose config`, pero no ejecutados.
- Un catálogo vacío no ejercita el procesamiento dentro del contenedor (límites de memoria y procesos aislados con *spawn*); eso queda para la auditoría de F10.
