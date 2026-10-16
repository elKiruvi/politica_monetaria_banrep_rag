# ADR 0005 — Importación local de PDF oficiales

**Decisión:** `banrep-rag importar ARCHIVO.pdf…` incorpora PDF que la persona obtuvo legítimamente (p. ej. descargados con su navegador desde el sitio del Banco) cuando la descarga automática está bloqueada. Es una vía de **continuidad**, no una sincronización.

**Cómo se identifica:** el periodo se deduce del metadato `/Title` del PDF y del texto de la portada (dos primeras páginas); deben concordar y al menos uno debe existir. `--periodo AAAA-MM` solo confirma, nunca sustituye a la evidencia. El PDF se valida igual que una descarga (firma, apertura estricta, cifrado, páginas, ≤ 30 MB) y se copia a `raw/<id>-<sha256>.pdf`. Por defecto se consultan en Crossref los metadatos del periodo; con `--sin-red` quedan marcados como no verificados.

**Procedencia honesta:** el registro guarda `method: local_import`, el nombre del archivo (nunca la ruta personal), la evidencia de identificación, `metadata_verified` y `verified_against_source: false`. `importar --activar` crea un catálogo con `selection.mode = local_import`, que `estado` muestra como «importación local (no equivale a los últimos N informes)». `sincronizar --usar-importados` solo recurre a un PDF importado cuando la **primera** descarga de ese informe falla; el snapshot sigue siendo `latest_n` con metadatos verificados y cada registro declara su método de adquisición.

**Límites:** no se puede probar que el archivo importado sea idéntico al publicado hoy (no hay hash oficial). Un archivo con título y portada falsificados pasaría la identificación: la importación confía en quien la ejecuta. **Alternativa descartada:** aceptar cualquier PDF con un periodo indicado a mano.
