# ADR 0007 — Índice léxico FTS5, manifiesto y activación atómica

**Fecha:** 18 de octubre de 2026. **Estado:** aceptada (F5A, issue #6).

## Contexto

F5 necesita un recuperador local evaluable antes de añadir embeddings. El índice debe consumir solo fragmentos verificados (H2–H5), conservar la evidencia económica (calidad, celdas atribuibles, `table_id`) y no publicar nunca un estado parcial.

## Decisión

- **Corpus verificado** (`corpus.py`): se lee el snapshot activo y, por cada informe, `ProcessedStore.load_verified` exige el PDF del almacén con su SHA-256, una caché válida para las versiones vigentes (reproducción de fragmentos desde las unidades) y los mismos metadatos del snapshot. Cada fragmento se revalida (`IndexedChunk.from_dict`): identificadores, tipo y calidad conocidos, páginas dentro del PDF, celdas solo con `structured_cells`, `table_id` en cuadros y notas, notas ligadas a un cuadro existente, identificadores únicos en el corpus. Un solo informe inválido impide indexar.
- **SQLite FTS5** con tokenizador `unicode61 remove_diacritics 2`: «inflacion» encuentra «inflación». Solo se normaliza la columna indexada; el texto citado es el original. `%` se indexa como «por ciento» (así «3,5 %» y «3,5 por ciento» coinciden). Cada cifra es **un solo token** (`tokenchars '_'`): «3,5» → `3_5`; el punto entre dígitos (miles o numeración) → `3__5`. Así «3,5» no coincide con cifras vecinas de una fila («4,9 | 25,7» no satisface «9,25») ni con «Gráfico 3.5». En la primera versión las cifras se buscaban como frase de dígitos y la revisión independiente midió hasta 25 de 50 resultados sin la cifra literal; ahora, «9,25» devuelve 13 resultados y todos la contienen.
- **Consultas seguras:** el texto del usuario nunca se pasa como sintaxis FTS5. Se extraen palabras y cifras, se eliminan palabras vacías, cada término va entre comillas y se unen con `OR`; en consultas de hasta seis palabras se añade la frase completa para premiar la coincidencia exacta. Máximo 500 caracteres.
- **Puntuación:** `bm25(chunks_fts, 1.0, 0.5)` (texto y sección). FTS5 devuelve valores **más negativos para mejores resultados**; se ordena por `(bm25, chunk_id)` para que los empates sean deterministas. El valor se expone como `lexical_bm25` y no se suma con otras puntuaciones (la fusión híbrida usará rangos, RRF).
- **Filtros** (informe, periodo de referencia, sección sin distinguir tildes, tipo de unidad, calidad de evidencia) se aplican en SQL; `SearchFilters.matches` reproduce la misma semántica para las vías que no consultan SQLite.
- **Notas de cuadros:** se recuperan por `(report_id, table_id)` y tipo `table_notes`, nunca por título. No crean celdas atribuidas.
- **Índices versionados** (`<data>/indexes/`): cada construcción se hace en `.tmp-*`, se comprueba (`integrity-check` de FTS5 y `PRAGMA integrity_check`), se escribe `manifest.json` con el SHA-256 de cada artefacto y el directorio se renombra a `<sha256 del manifiesto>`; solo entonces se reemplaza `current.json` de forma atómica. Un directorio publicado no se modifica.
- **Manifiesto:** esquema, formato, `build_key`, fecha UTC, snapshot del catálogo, modo de selección, informes con su hash de PDF, firma y versiones del procesamiento, número de fragmentos, huella de los identificadores (ordenados) y datos del archivo léxico; `vector` queda en `null` hasta F5B. Al abrir se verifican el hash del manifiesto, formatos, hash del archivo, metadatos internos de SQLite, número de filas y huella de identificadores. Se abre en modo `ro&immutable=1`.
- **Sin cambios, cambios y concurrencia:** `build_key` resume snapshot, firma de procesamiento, huella de fragmentos, hashes de PDF y formatos; si coincide con el índice activo válido no se reconstruye (`--forzar` lo hace). Un bloqueo de archivo serializa escritores. Justo antes de activar se vuelve a leer el snapshot: si cambió, no se activa nada. Los lectores resuelven el puntero una vez y abren un directorio inmutable; si una poda simultánea lo elimina, vuelven a leer el puntero.
- **Reversión:** se conservan el índice activo y hasta dos anteriores; `indexar --revertir` activa el anterior válido. Restos de construcciones interrumpidas se eliminan al iniciar la siguiente.
- **Contrato tipo ↔ calidad:** cada tipo de fragmento admite solo sus calidades (`paragraph` → `narrative_only`; `table` → `structured_cells` o `unattributed_values`; `table_raw` → `unattributed_values`; notas → `note_only`; leyendas → `caption_only`).
- **Puntero dañado:** `indexar` se niega a continuar y propone `--forzar`, que publica un índice nuevo y descarta el historial de reversión; los directorios publicados siguen validándose por su hash.
- **Integridad frente a vigencia:** `validar-indice` (código 3 si el índice no corresponde al catálogo activo) y `buscar` (solo aviso) informan por separado esa correspondencia y el estado efectivo de verificación de cada informe (`Catalog.verification`). Una nueva fecha de diagnóstico no exige reindexar.

## Evaluación inicial (F5A)

`evaluacion/consultas_recuperacion.json`: 14 consultas con respuesta y 3 sin respuesta, juicios verificados a mano sobre los cinco informes importados (páginas físicas y texto literal) y completados con pooling de los primeros resultados léxicos. Niveles: documento, página y evidencia (`evaluation.py`).

Métricas: recall cuenta los juicios cubiertos; en nDCG cada posición acredita un solo juicio (el de mayor grado), igual que la ordenación ideal, de modo que una lista perfecta vale 1.

Resultados de `banrep-rag evaluar --k 5` (índice léxico `fts5-2`, 1 566 fragmentos, 18-10-2026):

| Nivel | P@5 | R@5 | MRR | nDCG@5 |
|---|---|---|---|---|
| Documento | 0,929 | 0,911 | 0,929 | 0,897 |
| Página | 0,457 | 0,638 | 0,681 | 0,590 |
| Evidencia | 0,300 | 0,524 | 0,562 | 0,422 |

Sin filtro de informe (6 consultas): MRR documento 0,833, página 0,667, evidencia 0,639. **Precauciones:** 8 de las 14 consultas filtran por informe, así que en ellas el nivel documento vale 1 por construcción (por eso se informa aparte); y los juicios se completaron con pooling de BM25, lo que favorece a la vía léxica frente a la semántica. El nivel que importa para citar es **evidencia**. Latencia de búsqueda dentro del proceso: p50 ≈ 0,6 ms, p95 ≈ 1,6 ms (17 muestras); la CLI añade la verificación del SHA-256 del índice y de los PDF del catálogo para informar la vigencia. Construcción del índice: < 1 s. BM25 encuentra bien el informe, pero falla con paráfrasis («cierre de 2025» frente a «finales de 2025»), con preguntas sobre cuadros (prefiere párrafos que citan el cuadro) y con las consultas sin respuesta (siempre devuelve algo). Es la línea base que deberán superar la vía semántica y la fusión (F5B–F5C).

## Límites

- Cinco informes importados, no los últimos 12 sincronizados. Los juicios dependen de los cinco PDF locales y de las versiones actuales del procesamiento; `evaluar` falla si un juicio ya no tiene evidencia en el índice.
- La huella de un artefacto detecta corrupción, no autentica contra alguien que reescriba de forma coherente manifiesto, puntero y archivos.
- Entre la relectura del snapshot y el cambio de puntero no se toma el bloqueo del catálogo: una activación de catálogo en esos microsegundos dejaría un índice desfasado, que `validar-indice` detecta (código 3).
- «2025T1» o cifras con marca de nota pegada («indicador8») forman un solo token.
- Sin reordenamiento ni recuperación semántica todavía.
