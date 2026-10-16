# Diagnóstico inicial — 15 de octubre de 2026

## Resultado y límite actual

El checkout en `/home/elkiruvi/proyectos/politica_monetaria_banrep_rag` apunta al repositorio **original** `elKiruvi/rag-politica-monetaria-banrep` (HEAD `5af82fb`). La ruta solicitada `/proyectos/politica_monetaria_banrep_rag` no existe. Para conservar los cinco PDF versionados, el historial y el directorio `.atl/` no rastreado, el proyecto nuevo se creó en una **carpeta hermana**: `/home/elkiruvi/proyectos/politica_monetaria_banrep_rag-nuevo`. El original no se editó. En GitHub aún no existía el nuevo nombre al inspeccionarlo.

La página [histórica oficial](https://www.banrep.gov.co/es/publicaciones-investigaciones/informe-politica-monetaria/historico-informes) incluye, en el HTML inicial, julio y abril de 2026; cada sección mezcla un informe principal, presentación y recuadros. El informe de julio enlaza a `https://doi.org/rh7c`, que redirige al repositorio `repositorio.banrep.gov.co`. La paginación `?page=1` y el filtro `?field_date_format_value=2` devolvieron **Radware Bot Manager Block**; no son páginas vacías. No se eludirá ese control: aún no se han verificado los doce informes principales.

El `robots.txt` de Banrep no prohíbe la página histórica ni las rutas de informes; sí prohíbe rutas administrativas y de búsqueda. Esto no autoriza a eludir restricciones adicionales de acceso ni implica licencia para redistribuir PDF.

## Auditoría del original

El historial contiene un notebook con 32 celdas (14 de código), README y cinco PDF de enero de 2025 a enero de 2026. No hay archivo de licencia en el original. El notebook sube PDF manualmente a Colab, asocia fechas en un diccionario fijo, extrae texto con PyMuPDF y descarta líneas aisladas de uno a tres dígitos; esto puede perder datos. Crea/elimina el índice Pinecone en cada ejecución, usa embeddings OpenAI, clasificación NLI, expansión/HyDE, RRF, Cross-Encoder, filtro automático y evaluación LLM-as-a-judge. No hay una sincronización transaccional ni demostración en el README de resultados medidos en una instalación limpia. Se conserva la idea de recuperación fusionada y citas por página; no se heredan credenciales, servicios pagos ni complejidad sin evaluación.

## Entorno y verificación

- `uv 0.12.9`; Python de sistema 3.14.4 y Python 3.12.14 administrado por uv.
- Ollama 0.33.2 lista `qwen3:4b-instruct` y `embeddinggemma:latest` instalados; no se descargaron modelos. No se ha probado su inferencia en este proyecto.
- GitHub CLI autenticado como `elKiruvi`; no se imprimieron credenciales. Docker y `actionlint` no están instalados.
- El histórico respondió al HTML inicial; las consultas con parámetros activaron el bloqueo citado. Por tanto, **no** se puede afirmar actualmente la prueba de descubrimiento de 12 documentos.

## Próximos pasos verificables

1. Clasificador del informe principal con prueba contra presentaciones/recuadros; rechazo explícito de HTML de bloqueo.
2. Obtener acceso normal y autorizado a la paginación o una fuente pública alternativa oficialmente enlazada. Sin doce entradas comprobadas, no activar un índice incompleto.
3. Implementar descarga limitada, snapshots, extracción, recuperación, aplicación y evaluación por entregas independientes; cada gate debe aportar pruebas y distinguir integración sintética de prueba real.
