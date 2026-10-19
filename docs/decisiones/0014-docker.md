# ADR 0014 — Docker y Docker Compose

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F8B, issue #9).

## Decisión

- **Docker es opcional:** `uv` sigue siendo la forma principal de instalar y usar el proyecto.
- **Imagen** (`Dockerfile`):
  - Base `python:3.12-slim-bookworm`. `uv` 0.12.9 se monta solo durante la construcción (`RUN --mount`): no queda en ninguna capa, igual que su caché (`UV_NO_CACHE`).
  - Dependencias con `uv sync --frozen --no-dev --no-editable`, desde el mismo `uv.lock`. El proyecto se instala compilado en el entorno y `/app/src` se elimina, de modo que no hay recompilación en cada arranque con el sistema de archivos de solo lectura.
  - Las imágenes base se fijan por etiqueta, no por *digest*. La reproducibilidad completa queda para F9, junto con fijar las acciones por SHA.
  - Usuario sin privilegios (UID 10001) y `HEALTHCHECK` a `/api/v1/health`.
  - **No contiene PDF, índices, datos ni secretos:** `.dockerignore` excluye `data/`, `*.pdf`, `.env`, `tests/`, `docs/` y `.git/`. Los datos van en el volumen `/datos` (`BANREP_DATA_DIR`).
- **Red y seguridad:**
  - Dentro del contenedor el servicio escucha en `0.0.0.0` con `--permitir-red`. Compose publica el puerto **solo en `127.0.0.1`** del equipo.
  - Aun con `--permitir-red`, la API sigue exigiendo un `Host` local (protección contra DNS rebinding). Se pueden añadir nombres con `BANREP_ALLOWED_HOSTS`; «todos» solo con un `*` explícito. Esto corrige lo descrito en la ADR 0011, donde `--permitir-red` aceptaba cualquier `Host`.
  - El contenedor corre con sistema de archivos de solo lectura (más `tmpfs` en `/tmp`), `no-new-privileges`, sin capacidades (`cap_drop: ALL`) y con límites de 4 GB de memoria y 2 CPU. El procesamiento de un PDF ya se limita a 3 GB en su proceso aislado.
- **Ollama** (URL dentro del contenedor en `BANREP_DOCKER_OLLAMA_URL`, distinta de `BANREP_OLLAMA_URL` para que la del equipo, por ejemplo `127.0.0.1`, no se cuele):
  - Por defecto, el del equipo (`host.docker.internal`, con `host-gateway`).
    - **En Linux no funciona tal cual:** Ollama, instalado con systemd, escucha en `127.0.0.1` y no es accesible desde la red del puente de Docker.
    - No se recomienda `OLLAMA_HOST=0.0.0.0`, porque expone Ollama sin autenticación a la red local.
    - Alternativas seguras: usar el perfil `ollama`, o hacer que Ollama escuche también en la IP del puente (`OLLAMA_HOST=172.17.0.1`, o la de `ip addr show docker0`) con el puerto cerrado en el cortafuegos para la red local.
    - En Docker Desktop (macOS y Windows), `host.docker.internal` funciona sin cambios.
  - Perfiles `ollama` (CPU) y `ollama-gpu` (NVIDIA, requiere NVIDIA Container Toolkit) con `ollama/ollama:0.33.2` y volumen propio.
    - **Sin puerto publicado:** `banrep-rag` los usa por la red interna con el nombre `ollama` (alias también en el de GPU), y así no chocan con un Ollama ya instalado en el equipo.
    - Necesitan dos modelos: `embeddinggemma` (índice vectorial y consultas) y `qwen3:4b-instruct` (respuestas).
  - `BANREP_OLLAMA_LOCAL_HOSTS` declara `host.docker.internal` y `ollama` como el mismo equipo, para no emitir avisos de privacidad falsos. Si se usa `http://ollama:11434` sin el perfil, el nombre podría resolverse fuera; por eso la lista se puede ajustar con `BANREP_DOCKER_OLLAMA_LOCAL_HOSTS`.
- **Permisos del catálogo:**
  - El servicio corre con `BANREP_UID` y `BANREP_GID` (por defecto 10001). Un catálogo creado con `uv` pertenece a la persona y tiene modo `0700`, así que con el UID por defecto el contenedor no puede ni leerlo.
  - Por eso la configuración va en un `.env` junto a `compose.yaml`, copiado de `.env.example` y ajustado con `id -u`/`id -g`. Ahí también se elige la carpeta (`BANREP_DATOS`, por defecto `./data/catalogo`), que **debe existir**: si falta, Docker la crea como root y el contenedor no puede escribir en ella.

## Uso

```sh
cp .env.example .env            # y ajustar BANREP_UID/BANREP_GID con id -u / id -g
mkdir -p data/catalogo
docker compose build
docker compose up -d            # web y API en http://127.0.0.1:8000
docker compose run --rm banrep-rag banrep-rag importar /datos/entrada/informe.pdf --activar
docker compose run --rm banrep-rag banrep-rag actualizar --continuar-si-falla

# Ollama en contenedor: BANREP_DOCKER_OLLAMA_URL=http://ollama:11434 en .env
docker compose --profile ollama up -d                    # o --profile ollama-gpu
docker compose exec ollama ollama pull embeddinggemma    # una vez; con GPU: exec ollama-gpu
docker compose exec ollama ollama pull qwen3:4b-instruct # una vez, ~2,5 GB
```

## Verificación

- En el equipo de desarrollo **no hay Docker** y no se instaló, porque sería un cambio global no autorizado.
- La verificación real corre en GitHub Actions (`.github/workflows/docker.yml`) en cada PR que toca la imagen y en `main`:
  - `docker compose config` y la construcción de la imagen.
  - Que la imagen no contenga PDF (búsqueda como root en todo el sistema de archivos) ni `.sqlite` o `.npy`, salvo los `.npy` de prueba que trae numpy en su propio paquete.
  - Que no queden `uv` ni `/app/src`, y que la imagen use un usuario distinto de root.
  - `docker compose up` sobre un catálogo vacío hasta el estado *healthy*.
  - `/api/v1/health` 200, `/api/v1/ready` 503 (sin índice no está lista), `/` 200 y un `Host` ajeno 400.

## Límites

- La prueba en GitHub Actions no incluye Ollama ni GPU: los perfiles `ollama` y `ollama-gpu` están validados por `docker compose config`, pero no ejecutados.
- El acceso al Ollama del equipo en Linux depende de su configuración (ver arriba) y no se prueba.
- Con `mem_limit: 4g`, el servidor con FAISS cargado más un proceso aislado (hasta 3 GB) podría agotar la memoria del contenedor.
- Un catálogo vacío no ejercita el procesamiento dentro del contenedor (límites de memoria y procesos aislados con *spawn*); eso queda para la auditoría de F10.
