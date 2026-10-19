# ADR 0015 — Adquisición asistida de la ventana y freno ante bloqueos

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada e implementada (`catalog.py`, `cli.py`).

## Contexto (diagnóstico del 19-10-2026, Ubuntu del usuario)

| Solicitud | Resultado |
|---|---|
| Crossref (ventana y último informe) | 200; último informe 2026-07 (`10.32468/inf-pol-mont-spa.tr3-2026`) |
| Portada del histórico y `robots.txt` del repositorio | 200 |
| DOI → ruta heredada `/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf` | 302 |
| Ruta heredada → `/server/api/core/bitstreams/<uuid>/content` | 302 |
| Paginación del histórico | reto Radware (`validate.perfdrive.com`) |
| Minutos después: portada del histórico y página de presentación del informe | redirección a la portada y reto Radware |

`robots.txt` del repositorio mantiene `Disallow: /server`, y todo PDF del repositorio se entrega desde `/server/api/core/bitstreams/…/content`. Desde este equipo, la descarga automática se bloquea en la primera solicitud o tras unas pocas (ADR 0004). Un runner de GitHub Actions descargó un PDF (run 38936215976), pero los PDF no pueden salir del runner (no se publican en artefactos), así que eso no abastece el equipo local.

Antes de este cambio, `sincronizar` seguía pidiendo PDF tras un reto: con `--usar-importados`, cada informe de la ventana recibía su propio intento, hasta 12 solicitudes contra un servicio que ya había pedido verificación. Una segunda ejecución minutos después repetía el patrón. Ese comportamiento insiste justo después de una señal explícita de la fuente.

## Decisión

1. **Freno en la ejecución:** el primer `DownloadBlockedError` detiene todas las descargas de esa sincronización. Los informes restantes se componen con la descarga verificada anterior (aunque esté vencida), el PDF importado (con `--usar-importados`) o quedan como faltantes.
2. **Espera persistente de 12 h:** el último bloqueo queda en `revalidations.json` (ya existía, `error_type = blocked`). Mientras no hayan pasado 12 h, `sincronizar` no solicita PDF. `--reintentar-ahora` permite un intento manual deliberado; no hay reintentos automáticos.
3. **Ventana sin descargas:** `sincronizar --sin-descargar` (también en `actualizar`) consulta solo Crossref y activa la ventana `latest_n` con descargas verificadas y PDF importados. Cada registro conserva su método de adquisición (`download` o `local_import`). No registra intentos que no ocurrieron.
4. **Los importados solo se usan si la persona lo pide:** con `--usar-importados` o `--sin-descargar`, también durante la espera. Si falta un informe que ya está importado, el error lo señala («ya importado: use --usar-importados») en lugar de pedir que se importe otra vez. Un PDF descargado antes y conservado sin revalidar queda en el snapshot como `revalidation_pending`, nunca como `verified`. Si hubo un reto pero la ventana se completó con PDF anteriores o importados, `sincronizar` termina con un aviso explícito.
5. **Faltantes explícitos:** si falta algún informe, el error enumera **todos** los que faltan con su enlace oficial (DOI), en lugar de fallar en el primero. `banrep-rag faltantes` muestra el mismo estado sin intentar descargas.
6. **Importación de carpetas:** `importar CARPETA` revisa los PDF de primer nivel, sin subcarpetas.
   - Cada archivo se abre una sola vez con `O_NOFOLLOW | O_NONBLOCK`, y el tipo y el tamaño se validan sobre ese descriptor. Así se rechazan los enlaces simbólicos y los FIFO aunque aparezcan después de listar la carpeta.
   - Omite con aviso, sin mostrar rutas personales, los archivos que:
     - no son el informe principal;
     - no son PDF válidos;
     - no se pueden leer;
     - chocan con otro PDF importado del mismo periodo.
   - `faltantes` muestra hasta cuándo dura la espera tras un bloqueo. Un archivo indicado de forma explícita sigue fallando si no se puede importar.

El flujo recomendado cuando la fuente bloquea al equipo es:

```sh
banrep-rag faltantes                    # enlaces oficiales de los que faltan
# la persona abre cada enlace en su navegador y guarda el PDF
banrep-rag importar ~/Descargas         # identifica, verifica en Crossref y omite lo demás
banrep-rag sincronizar --sin-descargar  # activa la ventana de los últimos 12
```

## Consecuencias

- La ventana de los últimos 12 se puede completar sin eludir Radware. La descarga la hace una persona con su navegador; si el sitio le muestra un reto, lo resuelve ella como cualquier visitante. Esto no es automatización de navegador.
- El catálogo resultante es `latest_n` con metadatos verificados en Crossref, pero sus PDF **no** están verificados contra la fuente (`verified_against_source: false`). `estado` lo muestra así, y no se presenta como «doce informes sincronizados».
- La espera de 12 h es conservadora. Si la fuente se desbloquea antes, la persona puede usar `--reintentar-ahora` una vez.
- **Pendiente:** contrastar el SHA-256 de los PDF importados con una huella oficial observada (p. ej. calculada por un workflow de GitHub Actions que descargue, calcule el hash y borre el archivo, sin publicar el PDF). Requiere revisar antes la frecuencia y la política de `robots.txt` para `/server`.
- **Alternativas descartadas:**
  - Navegador automatizado o reutilizar cookies del reto: es eludir el antibot.
  - Proxies o rotación de IP: lo mismo.
  - Publicar los PDF desde Actions: redistribución y política del proyecto.
  - Reintentos con espera corta: insistencia ante una señal explícita.
