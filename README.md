# Informes de Política Monetaria — proyecto no oficial

Software libre para explorar, de forma local, los informes del [Banco de la República](https://www.banrep.gov.co/es/publicaciones-investigaciones/informe-politica-monetaria/historico-informes). No existe afiliación con el Banco. El software no proporciona asesoría financiera.

**Estado: en desarrollo.** Puede intentar sincronizar un catálogo local, pero aún no extrae texto ni responde preguntas. No hay un corpus precargado y los PDF del proyecto de referencia no se redistribuyen.

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

El diagnóstico de investigación, incluido el bloqueo de las consultas paginadas a la fuente, está en [docs/arquitectura/diagnostico_inicial.md](docs/arquitectura/diagnostico_inicial.md). Las entregas pendientes se describen en los issues. No se exige ningún proveedor comercial para instalar la base.

## Derechos

La licencia MIT cubre solo el código original de este repositorio. Los informes, marcas y demás material del Banco conservan sus respectivos derechos; este repositorio no incluye sus PDF.
