# Estado del desarrollo — checkpoint F5

**Actualizado:** 18 de octubre de 2026. **`main` al retomar:** `fd61cd68455156522d08a8d994941bf1a4e18311` (H5, PR #36); CI post-merge aprobado (run 39192394220: 441 aprobadas, 3 omitidas). H1–H5 fusionadas (#27–#31 cerrados). **Fase en curso:** F5 (#6), rama `feat/fts5-retrieval` (F5A).

## Resumen por fase

| Fase | Issue | Estado | Evidencia |
|---|---|---|---|
| F0 Auditoría | #1 | cerrada | ADR 0001 |
| F1 Base | #2 | cerrada | CI `Pruebas y calidad`, `uv.lock`, pre-commit |
| F2 Descubrimiento | #3 | **verificado con red real** (Crossref, N=12) | PR #20 |
| F3 Descarga y sincronización | #4 | implementada; 1 PDF descargado en GitHub Actions; **N=12 real pendiente** (Radware desde Ubuntu) | PR #14–#19, #22; run 38936215976 |
| Sprint de acceso | #18 | falta N=12 real y repetición | tabla del issue |
| F4 Procesamiento | #5 | **completada**; H1–H5 fusionadas (PR #32–#36) | ADR 0006 |
| F5 Indexación y recuperación | #6 | **F5A en curso**: corpus verificado, FTS5/BM25, manifiesto, activación atómica, reversión, `indexar`/`buscar`/`validar-indice`/`evaluar` | ADR 0007 |
| F6–F10 | #7–#11 | pendientes | — |

## Qué funciona hoy (comandos reales)

- `descubrir`, `sincronizar`, `importar`, `estado`, `diagnostico-fuentes` (F2–F3, ver README).
- `procesar`: cinco PDF importados → 1 566 fragmentos (304, 304, 303, 311 y 344; `max_chars=1400`), cuadros con calidad por unidad; enero de 2025 degradado.
- `indexar`, `buscar --modo lexico`, `validar-indice`, `evaluar`: índice léxico de los 1 566 fragmentos en < 1 s; búsqueda en ~1 ms. Línea base léxica (k=5, 14 consultas con respuesta): MRR documento 0,929, página 0,681 y evidencia 0,562 (ADR 0007).

## Evidencia de acceso (16-10-2026)

| Prueba | Ubuntu (usuario) | GitHub Actions |
|---|---|---|
| Crossref | 200, 12 informes | 200 |
| Portada del histórico | 200 | 200 |
| Histórico `?page=1` | reto Radware | reto Radware |
| DOI → ruta heredada | 302 | 302 |
| Ruta heredada → `/server/.../content` | 302 una vez; luego reto Radware por tasa | 302 |
| Descarga de 1 PDF (julio 2026) | bloqueada | **verificada**: 49 págs., 5 114 903 bytes, SHA-256 `27bec154…40b717` |
| Sincronización N=2 / N=12 real | bloqueada: reto Radware en `/server/.../content` desde la primera descarga | pendiente |

## Datos locales del equipo de desarrollo (no versionados)

- `data/catalogo/`: catálogo activo de **importación local** con 5 PDF (enero, abril, julio y octubre de 2025; enero de 2026), metadatos verificados en Crossref, `processed/` e `indexes/`. **No** es la ventana de los últimos 12.
- Ollama enumera `qwen3:4b-instruct`, `bge-m3`, `embeddinggemma` y `gemma3:4b`; no se ha medido su ejecución en este checkpoint.

## Pendiente y riesgos conocidos

1. **F5B:** proveedor de embeddings Ollama (comparar `embeddinggemma` y `bge-m3` con `evaluar`), FAISS en el mismo manifiesto (`vector`), validación de modelo y dimensión. **F5C:** fusión RRF, filtros coherentes y notas por `table_id` en la vía híbrida; reranker opcional medido.
2. **F3/#4, #18:** sincronización real N=12 y repetición sin descargas.
3. F4: enero de 2025 degradado (8 cuadros como `table_raw`); heurísticas ajustadas a 2025–2026; informes 2023–2024 sin validar.
4. Windows no probado; acciones fijadas por etiqueta (revisar en F9).

## Cómo retomar

```sh
git switch main && git pull --ff-only
uv sync --frozen && uv run --frozen pytest -q
uv run --frozen banrep-rag estado --data-dir data/catalogo
uv run --frozen banrep-rag indexar --data-dir data/catalogo && uv run --frozen banrep-rag evaluar --data-dir data/catalogo
```
Leer `CLAUDE.md` y las ADR 0001–0007; continuar F5B en una rama nueva desde `main`.
