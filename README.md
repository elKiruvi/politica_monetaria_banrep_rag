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

## Descubrimiento experimental

`uv run --frozen banrep-rag descubrir --max-reports 12` intenta localizar los doce informes principales en español. `--json` emite metadatos; `BANREP_MAX_REPORTS` cambia el valor predeterminado. Si el histórico bloquea las solicitudes, el comando sale con código 1 y **no** crea ni actualiza ningún índice. Se verificó la clasificación de los dos informes de la portada del HTML oficial, pero la paginación no fue accesible desde este entorno. No se debe interpretar esa muestra como un corpus de doce informes.

## Sincronización local experimental

`uv run --frozen banrep-rag sincronizar --max-reports 12 --data-dir ./datos` intenta descubrir, descargar y verificar los doce PDF principales antes de activar un catálogo en `./datos/current.json`. Por defecto usa `~/.local/share/banrep-rag`; `BANREP_DATA_DIR` cambia esa ruta. Las versiones anteriores permanecen en `snapshots/` y `raw/`. Ante un fallo previo a la activación, el catálogo anterior sigue activo; puede haber archivos nuevos no activos. No se redistribuyen estos PDF.

**No se ha comprobado una sincronización real de doce PDF:** Banrep bloqueó la paginación y el DOI comprobado redirigió fuera de los dominios autorizados. El comando falla cerrado; no intentes eludir sus controles ni interpretes un snapshot de catálogo como índice RAG. La [decisión de snapshots](docs/decisiones/0003-catalogo-snapshots.md) detalla sus límites.

El diagnóstico de investigación, incluido el bloqueo de las consultas paginadas a la fuente, está en [docs/arquitectura/diagnostico_inicial.md](docs/arquitectura/diagnostico_inicial.md). Las entregas pendientes se describen en los issues. No se exige ningún proveedor comercial para instalar la base.

## Derechos

La licencia MIT cubre solo el código original de este repositorio. Los informes, marcas y demás material del Banco conservan sus respectivos derechos; este repositorio no incluye sus PDF.
