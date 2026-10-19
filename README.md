# Informes de Política Monetaria — proyecto no oficial

Software libre para explorar, de forma local, los informes del [Banco de la República](https://www.banrep.gov.co/es/publicaciones-investigaciones/informe-politica-monetaria/historico-informes). No existe afiliación con el Banco. El software no proporciona asesoría financiera.

**Estado: en desarrollo.** Descubre los informes, sincroniza o importa un catálogo local y extrae texto, cuadros y fragmentos citables; aún no indexa ni responde preguntas. No hay un corpus precargado y los PDF del proyecto de referencia no se redistribuyen.

## Instalación de desarrollo

Se necesita `uv` y conexión para instalar dependencias. Python 3.12 se selecciona con `.python-version`.

```sh
uv sync --frozen
uv run --frozen banrep-rag diagnostico
uv run --frozen pytest -q
uv run --frozen ruff check .
```

## Descubrimiento de informes

`uv run --frozen banrep-rag descubrir --max-reports 12` identifica los doce informes principales en español más recientes con los metadatos que el Banco deposita en [Crossref](https://www.crossref.org/) (API pública, sin credenciales): título, DOI, fecha de publicación y URL del PDF en el repositorio institucional. `--json` emite todos los metadatos; `BANREP_MAX_REPORTS` cambia el valor predeterminado. El periodo se toma del título depositado y la fecha de publicación se marca con su origen (`crossref:published`). Presentaciones, recuadros, versiones en inglés y otros documentos se descartan; un registro ambiguo, duplicado o con URL no institucional hace fallar la consulta con código 1.

`--fuente historico` (o `BANREP_SOURCE=historico`) usa la página histórica del Banco, que solo permite verificar los dos informes de la portada porque la paginación está protegida por Radware. Si defines `BANREP_CONTACT_EMAIL`, ese correo se envía a Crossref como `mailto` (su «polite pool»); por defecto no se envía ningún dato personal.

**Verificado el 16-10-2026 desde Ubuntu:** `descubrir` devolvió julio, abril y enero de 2026; octubre, julio, abril y enero de 2025 y 2024; y octubre de 2023. La [ADR 0004](docs/decisiones/0004-fuentes-y-acceso.md) explica por qué no se usa la API del repositorio DSpace (`robots.txt` la prohíbe) ni se elude el antibot.

## Sincronización local experimental

`uv run --frozen banrep-rag sincronizar --max-reports 12 --data-dir ./datos` intenta descubrir, descargar y verificar los doce PDF principales antes de activar un catálogo en `./datos/current.json`. Por defecto usa `~/.local/share/banrep-rag`; `BANREP_DATA_DIR` cambia esa ruta. Muestra el avance por la salida de errores y desiste si la operación completa supera 30 minutos. Es incremental: un PDF verificado hace menos de `--revalidar-dias` (30) con los mismos metadatos no se vuelve a descargar; entre descargas reales espera `--pausa` segundos (10). `--dry-run` muestra qué cambiaría sin descargar nada. Las versiones anteriores permanecen en `snapshots/` y `raw/`. Ante un fallo previo a la activación, el catálogo anterior sigue activo; puede haber archivos nuevos no activos. No se redistribuyen estos PDF.

**No se ha comprobado una sincronización real de doce PDF:** desde el equipo de desarrollo, el repositorio institucional responde con una verificación antibot (Radware) a las solicitudes de PDF; la descarga falla con un mensaje explícito. El comando falla cerrado; no intentes eludir sus controles ni interpretes un snapshot de catálogo como índice RAG. La [decisión de snapshots](docs/decisiones/0003-catalogo-snapshots.md) detalla sus límites.

Tras el primer reto antibot, `sincronizar` no solicita ningún otro PDF en esa ejecución ni durante las 12 horas siguientes (`--reintentar-ahora` permite un intento manual). Si faltan informes, el error los enumera todos con su enlace oficial ([ADR 0015](docs/decisiones/0015-adquisicion-asistida.md)).

### Completar los últimos 12 con tu navegador

```sh
uv run --frozen banrep-rag faltantes                    # qué falta y su enlace oficial (DOI)
# abre cada enlace en tu navegador y guarda el PDF, p. ej. en ~/Descargas
uv run --frozen banrep-rag importar ~/Descargas         # importa los IPM y omite los demás PDF
uv run --frozen banrep-rag sincronizar --sin-descargar  # activa la ventana sin pedir PDF
```

`--sin-descargar` consulta solo Crossref y compone la ventana con descargas verificadas e importados; el catálogo es «los últimos 12», pero `estado` indica qué PDF se importaron y no se verificaron contra la fuente.

## Estado del catálogo e importación local

`uv run --frozen banrep-rag estado` muestra el catálogo activo, sus informes y cómo se obtuvo cada PDF. Si la descarga automática está bloqueada, puedes descargar los PDF con tu navegador desde el [sitio del Banco](https://www.banrep.gov.co/es/publicaciones-investigaciones/informe-politica-monetaria/historico-informes) e importarlos:

```sh
uv run --frozen banrep-rag importar ~/Descargas/informe-politica-monetaria-*.pdf --activar   # o una carpeta
```

El periodo se identifica con dos evidencias (portada con la serie junto al periodo, y título interno o ISSN de la serie), se rechazan presentaciones, anexos y otros documentos, y los metadatos se verifican en Crossref (`--sin-red` lo omite). Para sustituir un PDF ya importado del mismo periodo se usa `--reemplazar`. Un catálogo importado se marca como tal: **no** equivale a «los últimos 12 informes». Con `sincronizar --usar-importados`, un PDF importado sustituye a una descarga que falló o que no se intenta (tras un reto antibot o durante la espera de 12 h); con `--sin-descargar` se usa siempre que no haya una descarga verificada. Sin esas opciones nunca se usa. Detalles en la [ADR 0005](docs/decisiones/0005-importacion-local.md).

## Procesamiento de los informes

`uv run --frozen banrep-rag procesar` analiza los PDF del catálogo activo: separa columnas, títulos y secciones, reconstruye los cuadros con cada cifra junto a su fila, columna y unidad (y marca las proyecciones cuando el cuadro lo indica), conserva título y fuente de cada gráfico sin inventar sus datos, y genera fragmentos citables con página del PDF y página impresa. El resultado queda en `processed/` y se reutiliza mientras no cambien el PDF ni la versión del procesamiento (`--forzar` lo repite; `--procesos N` usa varios procesos). Cada informe muestra un resumen de calidad y se marca **degradado** si falta texto o fallan muchos cuadros. Detalles y mediciones en la [ADR 0006](docs/decisiones/0006-procesamiento-pdf.md).

## Índice y búsqueda

Requiere [Ollama](https://ollama.com) local con el modelo de embeddings (`ollama pull embeddinggemma`) para la búsqueda semántica e híbrida; la búsqueda léxica funciona sin él.

```sh
uv run --frozen banrep-rag indexar                     # léxico + vectores (embeddinggemma) y activación
uv run --frozen banrep-rag indexar --sin-embeddings    # solo léxico, sin Ollama
uv run --frozen banrep-rag buscar "expectativas de inflación"            # híbrida si hay vectores
uv run --frozen banrep-rag buscar "IPC total" --informe ipm-2025-07 --tipo table --modo lexico --json
uv run --frozen banrep-rag validar-indice              # integridad del índice y vigencia de las fuentes
uv run --frozen banrep-rag evaluar --modo hibrido      # métricas con consultas de relevancia verificada
uv run --frozen banrep-rag indexar --revertir          # reactiva el índice anterior
```

`indexar` solo usa fragmentos verificados (PDF por hash, caché vigente y metadatos del snapshot); si algo falla, incluido Ollama, el índice activo anterior se conserva. El índice queda ligado al modelo de embeddings (nombre, versión descargada y dimensión): cambiarlo exige reindexar. Cada resultado muestra informe, periodo, página del PDF y página impresa, sección, tipo de unidad, calidad de evidencia, número de celdas atribuibles, las señales de cada vía y, en filas de cuadros, sus notas vinculadas por `table_id`. Filtros: `--informe`, `--desde`/`--hasta` (AAAA-MM), `--seccion`, `--tipo` y `--calidad`. Variables: `BANREP_OLLAMA_URL` y `BANREP_EMBEDDING_MODEL` (`bge-m3` es la alternativa medida). Detalles y métricas en las ADR [0007](docs/decisiones/0007-indice-lexico.md) y [0008](docs/decisiones/0008-embeddings-y-busqueda-hibrida.md).

## Consultas con evidencia citada

```sh
ollama pull qwen3:4b-instruct      # una vez (~2,5 GB); el proyecto nunca lo descarga solo
uv run --frozen banrep-rag consultar "¿Cuál fue la proyección de inflación para finales de 2025 según el informe de julio de 2025?"
uv run --frozen banrep-rag consultar "¿Qué dijo el Banco sobre el precio del petróleo en octubre de 2025?" --json
```

La respuesta se redacta con Qwen local usando solo los fragmentos recuperados y se **verifica** antes de mostrarse: las citas deben existir y cada cifra debe figurar en la evidencia citada; si no, se muestra que fue rechazada junto con la evidencia revisada. Si la pregunta está fuera del alcance de los informes o la evidencia no basta, el sistema se abstiene. Un informe nombrado en la pregunta («informe de julio de 2025») restringe la búsqueda a ese informe. Límites y mediciones en la [ADR 0009](docs/decisiones/0009-respuestas-fundamentadas.md). Las respuestas no son asesoría financiera ni comunicaciones del Banco.

## Comparaciones y resúmenes

```sh
uv run --frozen banrep-rag comparar "¿Cómo cambiaron las proyecciones de inflación entre enero y julio de 2025?"
uv run --frozen banrep-rag comparar --informe 2025-10 --informe 2026-01 --indicador "producto interno bruto" --horizonte 2026
uv run --frozen banrep-rag resumir 2025-10
uv run --frozen banrep-rag resumir 2026-01 --tema "mercado laboral"
```

`comparar` no usa el modelo de lenguaje: toma solo celdas atribuidas de los cuadros, compara únicamente el mismo cuadro, fila, horizonte y unidad, calcula las diferencias en Python (en puntos porcentuales cuando la unidad es porcentual), distingue revisiones de proyección y datos observados, advierte diferencias de redondeo y cita cada valor con su página. `resumir` redacta con Qwen y verifica las cifras contra la evidencia del informe. Detalles en la [ADR 0010](docs/decisiones/0010-comparaciones-y-resumenes.md).

## Detección de publicaciones y actualización local

```sh
uv run --frozen banrep-rag detectar                 # 0 sin cambios, 10 cambios, 20-22 la fuente no respondió de forma válida
uv run --frozen banrep-rag actualizar --continuar-si-falla   # sincroniza (si se puede), procesa e indexa; 3 = parcial
```

El workflow «Detector de publicaciones» consulta Crossref dos veces por semana y abre un issue por cada informe nuevo, documento actualizado o corrección de metadatos, sin duplicarlos. No descarga PDF ni usa modelos. La actualización real ocurre en el equipo local, que debe estar encendido; puede programarse con cron. Detalles en la [ADR 0013](docs/decisiones/0013-detector-y-actualizacion.md).

## Docker (opcional)

```sh
cp .env.example .env && mkdir -p data/catalogo   # ajustar BANREP_UID/BANREP_GID con id -u / id -g
docker compose build
docker compose up -d                               # http://127.0.0.1:8000 con data/catalogo como volumen
```

La imagen no incluye PDF ni datos, corre sin privilegios y publica el puerto solo en este equipo. Puede usar un Ollama en contenedor con `--profile ollama` (CPU) o `--profile ollama-gpu` (NVIDIA). El Ollama del equipo también sirve, pero en Linux requiere que escuche en la IP del puente de Docker: no use `0.0.0.0`. `uv` sigue siendo la vía principal. Detalles en la [ADR 0014](docs/decisiones/0014-docker.md).

Con **Docker sin root** (*rootless*), el UID 0 del contenedor es tu propio usuario: usa `BANREP_UID=0` y `BANREP_GID=0`. Con `id -u`, el contenedor recibe un UID subordinado y no puede leer el catálogo (0700). Sigue sin capacidades y con `no-new-privileges`. En ese modo, el contenedor no alcanza el Ollama del equipo: la búsqueda funciona en modo léxico, y para generar respuestas se usa `--profile ollama`.

## Reportes descargables

```sh
uv run --frozen banrep-rag comparar "¿Cómo cambiaron las proyecciones de inflación entre enero y julio de 2025?" --exportar comparacion.pdf
uv run --frozen banrep-rag consultar "¿Qué dijo el informe de octubre de 2025 sobre el petróleo?" --exportar consulta.md
```

Formatos `.md`, `.html` (imprimible) y `.pdf`. Incluyen parámetros, informes con su vigencia, metodología, resultados con unidades y horizontes, citas, advertencias, estado del corpus y la declaración de proyecto no oficial. En la web, cada resultado ofrece «Descargar reporte»; en la API, `GET /api/v1/exports/{export_id}?format=md|html|pdf`. Detalles en la [ADR 0012](docs/decisiones/0012-reportes.md).

## Interfaz web y API local

```sh
uv run --frozen banrep-rag servir            # http://127.0.0.1:8000 ; esquema en /api/v1/openapi.json
curl -s http://127.0.0.1:8000/api/v1/ready
curl -s -X POST http://127.0.0.1:8000/api/v1/query -H 'Content-Type: application/json' \
  -d '{"question": "¿Qué dijo el informe de octubre de 2025 sobre el precio del petróleo?"}'
```

La interfaz web está en `http://127.0.0.1:8000`: preguntar, comparar, resumir y buscar evidencia, con el estado del corpus y de Ollama visible. No usa recursos externos.

Rutas `/api/v1`: `health`, `ready`, `status`, `reports`, `search`, `query`, `compare` y `summarize`. Usan el mismo servicio que la CLI, con los mismos filtros, verificaciones y avisos. Por seguridad escucha solo en localhost, rechaza cabeceras `Host` ajenas, no habilita CORS y no tiene autenticación: no la exponga a la red sin un proxy con autenticación (`--permitir-red` es explícito). Detalles en la [ADR 0011](docs/decisiones/0011-api-local.md).

El diagnóstico de investigación, incluido el bloqueo de las consultas paginadas a la fuente, está en [docs/arquitectura/diagnostico_inicial.md](docs/arquitectura/diagnostico_inicial.md). Las entregas pendientes se describen en los issues. No se exige ningún proveedor comercial para instalar la base.

## Estado y contribución

El estado detallado, la evidencia de acceso a las fuentes y el siguiente paso están en [docs/estado_desarrollo.md](docs/estado_desarrollo.md). Las normas para trabajar en el repositorio (incluidos agentes como Claude Code) están en [CLAUDE.md](CLAUDE.md).

## Derechos

La licencia MIT cubre solo el código original de este repositorio. Los informes, marcas y demás material del Banco conservan sus respectivos derechos; este repositorio no incluye sus PDF.
