# ADR 0013 — Detector de publicaciones y actualización local

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F8A, issue #9).

## Dos planos de operación

| Plano | Qué hace | Qué no hace |
|---|---|---|
| **GitHub** (`.github/workflows/detector.yml`, lunes y jueves) | Consulta Crossref y compara con `fuentes/informes_conocidos.json`. Abre un issue por cambio, sin duplicarlo. Falla si no pudo determinar si hay novedades. | No descarga PDF, no usa Ollama ni modelos y no publica datos ni artefactos. |
| **Local** (`banrep-rag actualizar`) | Sincroniza según la política de acceso, procesa e indexa. Cada paso conserva su protección: snapshot atómico, caché verificada e índice anterior ante fallos. | No se ejecuta si el equipo está apagado. No se instala como servicio: la persona decide si lo programa. |

## Decisión

- **Detector** (`detector.py`, `banrep-rag detectar`):
  - Hace una sola pasada por Crossref (una consulta por año, sin reintentos agresivos) y compara la ventana de N informes con una **referencia versionada** que solo guarda metadatos: identificador, periodo, título, DOI, fecha de publicación, URL, idioma, tipo de documento, URL de descarga, fecha de actualización del archivo y actualizaciones de la fuente.
  - Ignora campos volátiles, como la fecha de descubrimiento o la de depósito en Crossref, para no generar falsos cambios.
- **Clasificación y código de salida:**

  | Estado | Código |
  |---|---|
  | `sin_cambios` | 0 |
  | `cambios` | 10 |
  | `fuente_no_disponible` (bloqueo, HTTP 429 o 5xx) | 20 |
  | `respuesta_ambigua` (formato o clasificación inesperados, u otro código HTTP) | 21 |
  | `error_red` | 22 |

  Cada cambio es `nuevo_informe`, `documento_actualizado` (cambió la URL de descarga, la fecha del archivo o las actualizaciones de la fuente; el PDF puede ser otro) o `correccion_metadatos` (periodo, título, DOI, fecha, URL, idioma o tipo). Un documento actualizado informa también los metadatos que cambiaron con él. **Un fallo de acceso nunca se informa como «sin cambios».**
- **Idempotencia:** cada cambio lleva una clave estable: informe, tipo y huella de los metadatos nuevos. El workflow busca la clave en los issues abiertos y cerrados antes de crear otro. La referencia solo se reescribe con `--actualizar-referencia`, que sale con 0 aunque haya cambios. Nunca se reescribe con una respuesta inválida ni con una ventana menor que la referencia, porque eso haría que informes viejos aparecieran luego como nuevos. Sin referencia, `detectar` falla (código 2) en lugar de avisar de todos los informes como nuevos. Se actualiza en un PR revisado, no desde el workflow.
- **Actualización local:** `banrep-rag actualizar [--continuar-si-falla] [--usar-importados] [--sin-embeddings]` encadena `sincronizar`, `procesar` e `indexar`.
  - Si la sincronización falla (por ejemplo, por un reto antibot), se detiene.
  - Con `--continuar-si-falla`, procesa e indexa el catálogo activo, avisa de que no equivale a los últimos informes y termina con **código 3** (resultado parcial), para que un bloqueo persistente no pase inadvertido en una tarea programada.
  - Los errores de uso (por ejemplo, `--max-reports abc`) se rechazan antes de cualquier operación. Con `--sin-sincronizar` no se validan ni se usan las opciones de sincronización. Una ventana vacía o incompleta de la fuente se clasifica como `respuesta_ambigua`.
  - Para programarla, la persona puede añadir a su crontab (no se instala automáticamente):

    ```cron
    15 7 * * 2,5  cd /ruta/al/proyecto && uv run --frozen banrep-rag actualizar --continuar-si-falla >> ~/banrep-actualizar.log 2>&1
    ```

## Verificación

- **Crossref real (19-10-2026):** `detectar --actualizar-referencia` registró 12 informes, de julio de 2026 a octubre de 2023. Una segunda ejecución respondió «sin cambios» (código 0).
- **Pruebas sin red:**
  - Ventana sin cambios con campos volátiles distintos.
  - Informe nuevo, documento actualizado y corrección de metadatos.
  - Claves estables.
  - Bloqueo, HTTP 503, formato inesperado, error de red y causa de red dentro de `SourceError`.
  - Formato de la referencia.
  - Códigos de salida de la CLI y actualización de la referencia, que se rechaza ante un fallo.
  - `actualizar`, que se detiene o continúa según la opción.

## Límites

- El workflow solo **avisa**. La descarga y la indexación ocurren en el equipo local, sujetas a los controles del Banco; F3 sigue sin demostrar N=12 (#4 y #18).
- El índice de búsqueda de GitHub tarda unos segundos: dos ejecuciones manuales casi simultáneas podrían duplicar un issue (la concurrencia del workflow las serializa, pero no cubre ese retraso).
- Un informe retirado dentro de la ventana no se detecta como tal: el más antiguo que entra aparece como `nuevo_informe`.
- La primera ejecución del workflow en GitHub queda pendiente de verificarse tras fusionar este cambio (`workflow_dispatch`).
