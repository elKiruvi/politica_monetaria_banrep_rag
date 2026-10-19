# Estado del desarrollo — checkpoint F6

**Actualizado:** 19 de octubre de 2026. H1–H5 fusionadas (#27–#31 cerrados). **F5 cerrada** (#6): PR #37 y #38, `main` `763b655`, CI post-merge aprobado (run 39202051557). **Fase en curso:** F6 (#7), rama `feat/grounded-answers` (F6A: respuestas verificadas).

## Resumen por fase

| Fase | Issue | Estado | Evidencia |
|---|---|---|---|
| F0 Auditoría | #1 | cerrada | ADR 0001 |
| F1 Base | #2 | cerrada | CI `Pruebas y calidad`, `uv.lock`, pre-commit |
| F2 Descubrimiento | #3 | **verificado con red real** (Crossref, N=12) | PR #20 |
| F3 Descarga y sincronización | #4 | implementada; 1 PDF descargado en GitHub Actions; **N=12 real pendiente** (Radware desde Ubuntu) | PR #14–#19, #22; run 38936215976 |
| Sprint de acceso | #18 | falta N=12 real y repetición | tabla del issue |
| F4 Procesamiento | #5 | **completada**; H1–H5 fusionadas (PR #32–#36) | ADR 0006 |
| F5 Indexación y recuperación | #6 | **cerrada**: FTS5, FAISS, RRF híbrida, manifiesto, reversión, evaluación | ADR 0007, 0008 |
| F6 Generación y comparación | #7 | **F6A en curso**: `consultar` con Qwen, citas y verificación de cifras; F6B (comparaciones y resúmenes) pendiente | ADR 0009 |
| F7–F10 | #8–#11 | pendientes | — |

## Qué funciona hoy (comandos reales)

- `descubrir`, `sincronizar`, `importar`, `estado`, `diagnostico-fuentes` (F2–F3, ver README).
- `procesar`: cinco PDF importados → 1 566 fragmentos (304, 304, 303, 311 y 344; `max_chars=1400`), cuadros con calidad por unidad; enero de 2025 degradado.
- `indexar`, `buscar`, `validar-indice`, `evaluar`: índice léxico en < 1 s y vectores `embeddinggemma` en ~80 s (GPU). Nivel evidencia, k=5: léxica MRR 0,562; híbrida 0,687, R@5 0,714 (ADR 0007, 0008).
- `consultar`: respuestas con Qwen verificadas; 13/14 respondidas, abstención 3/3, p50 3 s (ADR 0009).

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

1. **F6B:** comparaciones entre informes calculadas en Python desde celdas atribuidas (mismo indicador, horizonte y unidad) y resúmenes trazables; después F7 (API, web, reportes). Reranker diferido a F9.
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
Leer `CLAUDE.md` y las ADR 0001–0009; continuar con F6B en una rama nueva desde `main`.
