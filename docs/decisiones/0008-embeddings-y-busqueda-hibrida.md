# ADR 0008 — Embeddings locales, FAISS y búsqueda híbrida RRF

**Fecha:** 18 de octubre de 2026. **Estado:** aceptada (F5B–F5C, issue #6).

## Decisión

- **Proveedor** (`embeddings.py`): Ollama local (`http://127.0.0.1:11434`, configurable con `BANREP_OLLAMA_URL`; una URL no local muestra un aviso de que los textos saldrán del equipo). Se llama a `/api/embed` con `truncate: false`: un texto que excede el contexto del modelo es un error, nunca se recorta en silencio (comprobado con Ollama 0.33.2). Lotes de 16; cada lote se valida (cantidad, dimensión constante, valores finitos, vector no nulo) y se normaliza por L2 en float32.
- **Identidad del modelo:** proveedor, nombre, *digest* de `/api/tags`, dimensión medida con una consulta de prueba y plantillas de texto (`embeddinggemma`: `title: {sección} | text: …` y `task: search result | query: …`; `bge-m3`: sección + texto). Todo entra en el manifiesto (`vector`) y en `build_key`: cambiar de modelo, de versión descargada o de plantilla obliga a reconstruir, y una consulta con otro modelo se rechaza («reconstruya el índice»). El modelo de generación no forma parte: cambiarlo no requiere reindexar.
- **FAISS** (`vector.py`, `faiss-cpu` MIT): `IndexFlatIP` exacto sobre vectores L2-normalizados (= coseno). El corpus es pequeño (1 566 × 768 float32 ≈ 4,8 MB) y un índice aproximado no aporta. Los vectores se guardan en `vectors.npy` y se leen con `allow_pickle=False`; el índice FAISS se reconstruye en memoria al abrir, de modo que no se deserializa ningún formato de FAISS. Antes de leer se verifica el SHA-256 del manifiesto y que el orden de filas coincida con el del índice léxico (`ordered_chunk_ids_sha256`; la fila *i* es el `rowid` *i + 1*).
- **Filtros coherentes:** la vía semántica obtiene del SQLite los `rowid` que cumplen los filtros (el mismo SQL que la vía léxica) y busca con `IDSelectorBatch`; ambas vías ven exactamente el mismo subconjunto.
- **Fusión** (`fusion.py`): Reciprocal Rank Fusion con `rrf_k = 60`, pesos 1 y 1, y `candidate_k = 50` por vía. Solo se usan posiciones; BM25 y coseno se informan por separado (`lexical_bm25`, `semantic_similarity`, sus posiciones y `fused_score`) y nunca se suman. Los empates se ordenan por la mejor posición y luego por `chunk_id`.
- **CLI:** `indexar` usa `embeddinggemma` por defecto (`--modelo-embeddings` o `BANREP_EMBEDDING_MODEL` para otro; `--sin-embeddings` construye solo el índice léxico sin Ollama). `buscar` y `evaluar` tienen `--modo auto|lexico|semantico|hibrido`; `auto` usa la vía híbrida si el índice tiene vectores. Si Ollama falla durante la indexación, el índice activo no cambia.
- **Reordenamiento:** no se implementa todavía. Ollama no ofrece una API de reordenamiento y no hay un modelo de reranking local instalado; añadir otro motor (p. ej. `sentence-transformers` con un cross-encoder) supone unos 2 GB de dependencias y memoria adicional. Se reevaluará con más consultas en F9, y se mantendría desactivado por defecto si el beneficio es marginal.

## Evaluación (18-10-2026)

Equipo: i5-10400, 16 GB de RAM, GTX 1660 SUPER (6 GB), Ollama 0.33.2. Corpus: **cinco informes importados** (1 566 fragmentos), no los últimos 12. Consultas: `evaluacion/consultas_recuperacion.json` (14 con respuesta y 3 sin respuesta), k = 5.

Nivel evidencia (el fragmento correcto con el texto literal esperado):

| Vía | Modelo | P@5 | R@5 | MRR | nDCG@5 | MRR sin filtro de informe |
|---|---|---|---|---|---|---|
| Léxica | — | 0,300 | 0,524 | 0,562 | 0,422 | 0,639 |
| Semántica | embeddinggemma | 0,343 | 0,575 | 0,556 | 0,539 | 0,597 |
| Semántica | bge-m3 | 0,286 | 0,616 | 0,604 | 0,570 | 0,617 |
| **Híbrida** | **embeddinggemma** | **0,400** | **0,714** | **0,687** | **0,625** | **0,722** |
| Híbrida | bge-m3 | 0,357 | 0,654 | 0,667 | 0,611 | 0,667 |

Nivel página, vía híbrida: MRR 0,792 (embeddinggemma) y 0,756 (bge-m3), frente a 0,681 de la léxica.

| Modelo | Dimensión | Contexto | Indexar 1 566 fragmentos | Memoria del modelo | Latencia híbrida p50 / p95 | Licencia |
|---|---|---|---|---|---|---|
| embeddinggemma (300 M, BF16) | 768 | 2 048 tokens | 80 s | 681 MB (GPU) | 58 / 111 ms | Gemma Terms of Use |
| bge-m3 (567 M, F16) | 1 024 | 8 192 tokens | 468 s | 664 MB (GPU) | 62 / 167 ms | MIT |

La memoria del proceso `banrep-rag` durante la indexación es de ~115 MB (RSS máximo).

**Elección:** `embeddinggemma` con búsqueda híbrida por defecto. Es la mejor configuración medida y casi 6 veces más rápida al indexar, aunque la diferencia de calidad con `bge-m3` es pequeña y la muestra (14 consultas) no permite afirmarla con confianza. `bge-m3` sigue disponible y es preferible si su licencia MIT importa o si los fragmentos crecen más allá de unos 2 000 tokens. El modelo no se distribuye con el proyecto: cada persona lo descarga con `ollama pull` y acepta sus condiciones.

## Límites

- Los juicios se completaron con pooling léxico; favorecen a BM25, no a la vía semántica.
- La vía semántica y la híbrida necesitan que Ollama esté en ejecución en cada consulta; la léxica no.
- La CI ordinaria usa embeddings simulados deterministas; la prueba con Ollama real es opcional (`BANREP_OLLAMA_LIVE=1`).
