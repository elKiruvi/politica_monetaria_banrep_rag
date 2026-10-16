# Informes de Política Monetaria — proyecto no oficial

Software libre para explorar, de forma local, los informes del [Banco de la República](https://www.banrep.gov.co/es/publicaciones-investigaciones/informe-politica-monetaria/historico-informes). No existe afiliación con el Banco. El software no proporciona asesoría financiera.

**Estado: en desarrollo.** Esta primera entrega instala una base reproducible; todavía no sincroniza informes ni responde preguntas. No hay un corpus precargado y los PDF del proyecto de referencia no se redistribuyen.

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

El diagnóstico de investigación, incluido el bloqueo de las consultas paginadas a la fuente, está en [docs/arquitectura/diagnostico_inicial.md](docs/arquitectura/diagnostico_inicial.md). Las entregas pendientes se describen en los issues. No se exige ningún proveedor comercial para instalar la base.

## Derechos

La licencia MIT cubre solo el código original de este repositorio. Los informes, marcas y demás material del Banco conservan sus respectivos derechos; este repositorio no incluye sus PDF.
