# CLAUDE.md — politica_monetaria_banrep_rag

RAG local y gratuito sobre los Informes de Política Monetaria (IPM) del Banco de la República. Proyecto no oficial. Estado y siguiente paso: [docs/estado_desarrollo.md](docs/estado_desarrollo.md).

## Idioma
Todo texto para personas en **español**: README, docs, mensajes de CLI y errores, issues, PR, commits (prefijo convencional `feat:`/`fix:`/`docs:`… y descripción en español), comentarios y docstrings. Identificadores de código en inglés.

## Repositorios
- Este repo: `elKiruvi/politica_monetaria_banrep_rag` (checkout en `/home/elkiruvi/proyectos/politica_monetaria_banrep_rag-nuevo`).
- `/home/elkiruvi/proyectos/politica_monetaria_banrep_rag` es el repo **original** (`rag-politica-monetaria-banrep`): solo referencia; **nunca** modificarlo ni hacer push. Sus 5 PDF se pueden leer o importar a `data/` (ignorado por Git).

## Comandos
```sh
uv sync --frozen
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest -q            # offline: tests/conftest.py bloquea la red
BANREP_LIVE=1 uv run --frozen pytest -q -k live   # opcional, consulta Crossref
BANREP_OLLAMA_LIVE=1 uv run --frozen pytest -q -k ollama   # opcional, Ollama local con embeddinggemma
uv run --frozen pre-commit run --all-files
uv run --frozen banrep-rag --help    # descubrir, sincronizar, faltantes, importar, estado, procesar, indexar, buscar, consultar, comparar, resumir, servir, detectar, actualizar, validar-indice, evaluar
```
Con `set -o pipefail` cuando se encadene `pytest … | tail`: sin él, un fallo pasa inadvertido.

## Módulos
`urls.py` (política de URL oficiales) · `source.py` (histórico, `ReportRecord`) · `crossref.py` (descubridor principal) · `download.py` (descarga segura) · `catalog.py` (snapshots, registro de adquisiciones, importación) · `importer.py` (identificación de PDF locales) · `isolation.py` (procesos aislados para PDF no confiables) · `layout.py` y `tables.py` (bloques y cuadros con pdfminer) · `processing.py`, `chunking.py`, `processed_store.py` (unidades, fragmentos, caché) · `retrieval.py`, `corpus.py`, `lexical.py`, `index_store.py` (fragmentos verificados, FTS5, manifiesto y activación atómica de índices) · `embeddings.py`, `vector.py`, `fusion.py` (Ollama, FAISS, RRF) · `generation.py`, `answering.py` (Qwen y respuestas verificadas) · `comparison.py` (comparaciones deterministas) · `service.py` (orquestación compartida), `api.py` (FastAPI local y web en `web/static`) y `reports.py` (reportes Markdown, HTML y PDF) · `detector.py` (publicaciones nuevas frente a `fuentes/informes_conocidos.json`) · `evaluation.py` (métricas con `evaluacion/`) · `diagnostics.py` · `cli.py`. Decisiones en `docs/decisiones/`.

## Reglas de fuentes y seguridad
- Descubrir con Crossref (`api.crossref.org`, prefijo `10.32468`). **No** usar la API DSpace (`/server`, prohibida por `robots.txt`) para descubrir. **Nunca** eludir Radware/antibot (sin cookies de reto, navegadores simulados, proxies ni reintentos agresivos); un reto = `*BlockedError` y fallo cerrado.
- Toda URL de descarga pasa por `urls.py`; los PDF se validan (`inspect_pdf`) y se analizan en `isolation.run_isolated`.
- No enviar el correo del usuario a ningún servicio (`BANREP_CONTACT_EMAIL` es opcional y lo define la persona).
- No subir PDF ni datos de `data/` al repositorio. El código es MIT; los informes conservan sus derechos.
- No inventar cifras: un cuadro ambiguo se guarda sin columnas (`table_raw`); los datos de gráficos no se extraen.

## Flujo de PR
- Rama desde `main` (o apilada si depende de otra), PR en español con objetivo, cambios, verificación real y riesgos.
- Fusionar solo con `Pruebas y calidad` en verde **para el SHA actual**, hilos resueltos y revisión independiente sin hallazgos altos. `gh pr merge/edit` fallan por la deprecación de Projects classic: usar `gh api -X PUT repos/elKiruvi/politica_monetaria_banrep_rag/pulls/N/merge -f merge_method=squash -f sha=<SHA>`.
- Sourcery omite PR cuya base no es `main`.
- El `.venv` tiene instalación editable que apunta al árbol de trabajo: para probar otra rama usar una copia con `git archive` y `sys.path` explícito.
- Nunca `--no-verify` en commits finales, `git reset --hard` sobre trabajo ajeno, `git clean -fdx`, force-push a `main` ni `--admin`.
