# ADR 0012 — Reportes descargables

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F7C, issue #8).

## Decisión

- **Origen:** un reporte se construye con el resultado ya verificado de `consultar`, `comparar` o `resumir` (`reports.py`). No vuelve a llamar al modelo ni recalcula nada.
- **Contenido común:**
  - título y fecha UTC;
  - parámetros (pregunta, informes, indicador, horizonte, cuadro);
  - informes utilizados, con periodo, título, fuente, adquisición y vigencia efectiva;
  - metodología propia de cada tipo;
  - resultados: la respuesta con su estado y motivo, o las filas comparadas con valor, tipo (proyección u observado) y página, más las diferencias con unidad y horizonte;
  - citas con página física e impresa y URL, y notas del cuadro en las comparaciones;
  - advertencias (corpus de importación local, informes no indexados, fuentes añadidas por la verificación);
  - estado del corpus y la declaración de proyecto no oficial.
- **Respuestas no publicadas:** si la respuesta se rechazó o la evidencia no bastó, la lista se titula «Evidencia revisada (no respalda ninguna respuesta publicada)», nunca «Citas». En las comparaciones truncadas (más de 40 filas) se advierte que faltan filas.
- **Lo que nunca incluye:** rutas locales, secretos ni el contenido completo de los PDF; solo las citas y la respuesta.
- **Formatos:**
  - **Markdown**: cada texto va en una sola línea, con el marcado escapado y sin listas iniciales. Un texto del modelo o de los PDF no puede crear encabezados, enlaces, definiciones de referencia ni HTML. Las marcas «[E1]» se conservan solo si no van seguidas de «(», «:» o «[».
  - **HTML imprimible:** autocontenido, con estilos propios y todo el texto escapado con `html.escape`.
  - **PDF**, con ReportLab 4.5 (BSD): fuentes estándar, tablas con anchos fijos y ajuste de palabras largas. Esas fuentes cubren cp1252, que incluye todo el español (tildes, ñ, ¿, «»). Unos pocos símbolos que quedan fuera se transliteran: «→» pasa a «->», «−» a «-» y «‹›» a «<>». Además, se normalizan por compatibilidad Unicode las letras matemáticas y los espacios y guiones especiales, y se eliminan los caracteres de ancho cero. Se descartó una fuente TTF embebida para no versionar binarios.
- **CLI:** `--exportar ARCHIVO.{md,html,pdf}` en `consultar`, `comparar` y `resumir`.
  - El destino se valida **antes** de la operación (extensión, que no sea un enlace simbólico, que el directorio exista, y `--sobrescribir` si ya existe), para no perder una respuesta que tarda en calcularse.
  - El resultado se muestra antes de escribir el reporte, y un fallo al guardar lo indica sin ocultar el resultado.
  - El temporal se crea con `mkstemp` (`O_EXCL`) en el mismo directorio, no sigue enlaces simbólicos, se reemplaza con `os.replace` y se elimina ante cualquier fallo o interrupción.
  - Una operación que falla no genera reporte.
- **API:**
  - Cada respuesta de `query`, `compare` y `summarize` incluye un `export_id`.
  - `GET /api/v1/exports/{export_id}?format=md|html|pdf` lo entrega como descarga (`Content-Disposition: attachment`), con un nombre generado por el servidor, sin datos del cliente.
  - Se guardan en memoria los 32 reportes más recientemente creados (cola FIFO); nada se escribe en disco.
  - El identificador es un UUID aleatorio de 32 caracteres hexadecimales; otro valor responde 422 o 404.
- **Web:** cada respuesta, resumen y comparación muestra «Descargar reporte: Markdown · HTML imprimible · PDF», con enlaces del mismo origen.

## Verificación

- **Pruebas:**
  - Partes obligatorias del reporte y ausencia de rutas locales.
  - Escape de HTML con texto hostil.
  - PDF válido con el texto español extraíble (`pdftotext`).
  - Comparación con valores, diferencias y metodología.
  - Formatos desconocidos rechazados.
  - CLI: no sobrescribe sin permiso, valida la extensión y no deja reporte si la operación falla.
  - API: las tres descargas, la cabecera `Content-Disposition` y los identificadores desconocidos o inválidos.
- **Exportación real** con los cinco informes importados: comparación en PDF (IPC total, +0,65 pp), y consulta sobre el petróleo (octubre de 2025) en Markdown y HTML, con citas a las págs. 13-14 y 19 y sin rutas locales.

## Límites

- Los reportes viven en la memoria del servidor: un reinicio o 32 operaciones nuevas los descartan.
- El PDF no incrusta fuentes; los símbolos fuera de cp1252 se transliteran o se normalizan, y los emoji o CJK no tendrían representación (no aparecen en el corpus).
- Una celda más alta que una página impediría componer el PDF; con los datos reales no ocurre, porque los valores y títulos son cortos.
- En las consultas, la adquisición se deduce de la vigencia (las citas no llevan el método); la vigencia se muestra en texto legible.
