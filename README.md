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

El diagnóstico de investigación, incluido el bloqueo de las consultas paginadas a la fuente, está en [docs/arquitectura/diagnostico_inicial.md](docs/arquitectura/diagnostico_inicial.md). Las entregas pendientes se describen en los issues. No se exige ningún proveedor comercial para instalar la base.

## Derechos

La licencia MIT cubre solo el código original de este repositorio. Los informes, marcas y demás material del Banco conservan sus respectivos derechos; este repositorio no incluye sus PDF.
