# ADR 0007 — Índice léxico FTS5, manifiesto y activación atómica

**Fecha:** 18 de octubre de 2026. **Estado:** aceptada (F5A, issue #6).

## Contexto

F5 necesita un recuperador local evaluable antes de añadir embeddings. El índice debe consumir solo fragmentos verificados (H2–H5), conservar la evidencia económica (calidad, celdas atribuibles, `table_id`) y no publicar nunca un estado parcial.

## Decisión

- **Corpus verificado** (`corpus.py`): se lee el snapshot activo y, por cada informe, `ProcessedStore.load_verified` exige el PDF del almacén con su SHA-256, una caché válida para las versiones vigentes (reproducción de fragmentos desde las unidades) y los mismos metadatos del snapshot. Cada fragmento se revalida (`IndexedChunk.from_dict`): identificadores, tipo y calidad conocidos, páginas dentro del PDF, celdas solo con `structured_cells`, `table_id` en cuadros y notas, notas ligadas a un cuadro existente, identificadores únicos en el corpus. Un solo informe inválido impide indexar.
- **SQLite FTS5** con tokenizador `unicode61 remove_diacritics 2`: «inflacion» encuentra «inflación». Solo se normaliza la columna indexada; el texto citado es el original. `%` se indexa como «por ciento» (así «3,5 %» y «3,5 por ciento» coinciden). Las cifras se consultan como frase: «3,5» exige «3» y «5» contiguos, de modo que no coincide con «3» y «5» sueltos.
- **Consultas seguras:** el texto del usuario nunca se pasa como sintaxis FTS5. Se extraen palabras y cifras, se eliminan palabras vacías, cada término va entre comillas y se unen con `OR`; en consultas de hasta seis palabras se añade la frase completa para premiar la coincidencia exacta. Máximo 500 caracteres.
- **Puntuación:** `bm25(chunks_fts, 1.0, 0.5)` (texto y sección). FTS5 devuelve valores **más negativos para mejores resultados**; se ordena por `(bm25, chunk_id)` para que los empates sean deterministas. El valor se expone como `lexical_bm25` y no se suma con otras puntuaciones (la fusión híbrida usará rangos, RRF).
- **Filtros** (informe, periodo de referencia, sección sin distinguir tildes, tipo de unidad, calidad de evidencia) se aplican en SQL; `SearchFilters.matches` reproduce la misma semántica para las vías que no consultan SQLite.
- **Notas de cuadros:** se recuperan por `(report_id, table_id)` y tipo `table_notes`, nunca por título. No crean celdas atribuidas.
- **Índices versionados** (`<data>/indexes/`): cada construcción se hace en `.tmp-*`, se comprueba (`integrity-check` de FTS5 y `PRAGMA integrity_check`), se escribe `manifest.json` con el SHA-256 de cada artefacto y el directorio se renombra a `<sha256 del manifiesto>`; solo entonces se reemplaza `current.json` de forma atómica. Un directorio publicado no se modifica.
- **Manifiesto:** esquema, formato, `build_key`, fecha UTC, snapshot del catálogo, modo de selección, informes con su hash de PDF, firma y versiones del procesamiento, número de fragmentos, huella de los identificadores (ordenados) y datos del archivo léxico; `vector` queda en `null` hasta F5B. Al abrir se verifican el hash del manifiesto, formatos, hash del archivo, metadatos internos de SQLite, número de filas y huella de identificadores. Se abre en modo `ro&immutable=1`.
- **Sin cambios, cambios y concurrencia:** `build_key` resume snapshot, firma de procesamiento, huella de fragmentos, hashes de PDF y formatos; si coincide con el índice activo válido no se reconstruye (`--forzar` lo hace). Un bloqueo de archivo serializa escritores. Justo antes de activar se vuelve a leer el snapshot: si cambió, no se activa nada. Los lectores resuelven el puntero una vez y abren un directorio inmutable; si una poda simultánea lo elimina, vuelven a leer el puntero.
- **Reversión:** se conservan el índice activo y hasta dos anteriores; `indexar --revertir` activa el anterior válido. Restos de construcciones interrumpidas se eliminan al iniciar la siguiente.
- **Integridad frente a vigencia:** `validar-indice` y `buscar` informan por separado si el índice corresponde al catálogo activo (código 3 si no) y el estado efectivo de verificación de cada informe (`Catalog.verification`). Una nueva fecha de diagnóstico no exige reindexar.

## Evaluación inicial (F5A)

`evaluacion/consultas_recuperacion.json`: 14 consultas con respuesta y 3 sin respuesta, juicios verificados a mano sobre los cinco informes importados (páginas físicas y texto literal) y completados con pooling de los primeros resultados léxicos. Niveles: documento, página y evidencia (`evaluation.py`).

Resultados de `banrep-rag evaluar --k 5` (índice léxico, 1 566 fragmentos, 18-10-2026):

| Nivel | P@5 | R@5 | MRR | nDCG@5 |
|---|---|---|---|---|
| Documento | 0,929 | 0,925 | 0,929 | 0,801 |
| Página | 0,429 | 0,638 | 0,681 | 0,593 |
| Evidencia | 0,300 | 0,509 | 0,574 | 0,436 |

Latencia por consulta: p50 ≈ 0,8 ms, p95 ≈ 1,4 ms (17 muestras; equipo de desarrollo). Construcción del índice: < 1 s. BM25 encuentra bien el informe, pero falla con paráfrasis («cierre de 2025» frente a «finales de 2025»), con preguntas sobre cuadros (prefiere párrafos que citan el cuadro) y con las consultas sin respuesta (siempre devuelve algo). Es la línea base que deberán superar la vía semántica y la fusión (F5B–F5C).

## Límites

- Cinco informes importados, no los últimos 12 sincronizados. Los juicios dependen de los cinco PDF locales y de las versiones actuales del procesamiento; `evaluar` falla si un juicio ya no tiene evidencia en el índice.
- La huella de un artefacto detecta corrupción, no autentica contra alguien que reescriba de forma coherente manifiesto, puntero y archivos.
- Sin reordenamiento ni recuperación semántica todavía.
