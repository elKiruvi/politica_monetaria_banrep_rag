# ADR 0004 — Fuentes de descubrimiento y política de acceso

**Fecha:** 16 de octubre de 2026. **Estado:** aceptada e implementada (`urls.py`, `crossref.py`).

## Evidencia (Ubuntu local, `httpx` sin seguir redirecciones, solicitudes espaciadas)

| Solicitud | Resultado |
|---|---|
| Portada del histórico (`www.banrep.gov.co`) | 200 HTML; solo incluye los dos informes más recientes |
| Histórico con `?page=1` (diagnóstico previo) | Radware Bot Manager Block |
| `https://doi.org/rh7c` | 301 → `https://doi.org/10.32468/INF-POL-MONT-SPA.TR3-2026` |
| API de handles de doi.org para el DOI canónico | URL registrada `https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf` |
| `repositorio.banrep.gov.co/robots.txt` | `Disallow: /server` (API REST y OAI de DSpace) para todo agente; publica sitemaps |
| Colección `f8573ac9-…` (1.ª solicitud) | 200 HTML SSR con 20 enlaces a fichas |
| Ficha `/items/…`, `/bitstreams/…/download` y luego la colección | 302 → `validate.perfdrive.com` (Radware) |
| Crossref (`api.crossref.org`, prefijo `10.32468`) | 200: título, editor «Banco de la República», fecha de publicación y URL primaria del PDF; incluye los 12 informes principales esperados (2026-07 … 2023-10) |

Los DOI históricos no siempre codifican el periodo (p. ej. `…spa.tr3-2021` corresponde a octubre de 2021 y `…eng.tr3.-2021` tiene título en español). El **título** depositado es la referencia del periodo; el DOI es un identificador, no una fecha.

## Decisión

1. **No usar la API DSpace** (`/server/api/...`) para descubrir informes: `robots.txt` lo prohíbe. Tampoco se usa OAI-PMH (`/server/oai`).
2. **Nunca eludir Radware**: no se envían cookies de verificación, no se emula un navegador, no se rotan IP ni se reintenta para «pasar» el reto. Una redirección a `*.perfdrive.com` produce `SourceBlockedError`/`DownloadBlockedError` y la sincronización falla cerrada.
3. **Descubrimiento principal:** metadatos que el Banco deposita en Crossref (API pública, documentada, sin credenciales; metadatos bajo CC0). Se consultan ventanas anuales completas por fecha de publicación (`type:report`, prefijo `10.32468`), sin búsqueda por relevancia; Crossref no permite cursor con orden por fecha. Se lee también el año siguiente al actual (una fecha futura es un error). La ventana se cierra cuando el N-ésimo periodo es posterior al inicio del año consultado: como la publicación nunca precede al periodo, ningún registro más antiguo puede desplazarla; para comprobarlo se lee un año adicional y cualquier fecha anómala allí hace fallar la consulta. Títulos se normalizan a NFKC; uno que parezca de la serie (sin tildes, mayúsculas, caracteres de formato) pero no encaje en el patrón estricto falla en lugar de descartarse. Las fechas parciales se validan con la precisión disponible y no se guardan como fecha de publicación. Un título español con DOI `-eng.` se considera ambiguo: por eso, a 16-10-2026, ventanas de más de unos 15 informes llegan al registro de julio de 2021 y fallan de forma explícita. Duplicados, títulos ambiguos, URL no institucionales o años incompletos fallan cerrados. Las correcciones Crossmark (`updated-by`) se guardan en `source_updates`. El histórico de `www.banrep.gov.co` queda como fuente alternativa independiente (`--fuente historico`).
4. **Descarga:** se inicia solo en un DOI de doi.org (`10.32468/…` o DOI corto), un PDF de `www.banrep.gov.co` o la ruta heredada `repositorio.banrep.gov.co/bitstream/handle/20.500.12134/<n>/<archivo>` (con o sin `.pdf`). Los saltos `/bitstreams/<uuid>/download` y `/server/api/core/bitstreams/<uuid>/content` se aceptan **solo como redirecciones emitidas por el repositorio** para un documento concreto ya identificado; el proyecto no solicita `/server` por iniciativa propia. La descarga queda limitada a la ventana solicitada, en serie y con caché por hash.
5. **Política de URL** en `urls.py`: HTTPS, host exacto (sin punto final, mayúsculas, punycode ni `@`), sin puerto, credenciales, parámetros ni fragmentos en descargas, sin segmentos vacíos o `.`/`..` (también codificados), sin `/` o `\` codificados, sin caracteres de control ni espacios, y UUID en minúsculas. La validación del contenido (MIME, `%PDF-`, apertura con `pypdf`, tamaño, páginas y SHA-256) sigue separada en `download.py`.

## Consecuencias y riesgos

- Desde este equipo, **la descarga real sigue bloqueada** por Radware en el repositorio. Se debe comprobar desde un runner de GitHub Actions (otra IP) y, si persiste, usar la importación local de PDF obtenidos legítimamente, con procedencia `local_import`.
- Seguir las redirecciones de un DOI hasta `/server/.../content` es lo que hace un navegador al abrir el DOI; se considera recuperación dirigida de un documento, no rastreo. Si el Banco objeta o cambia sus condiciones, se desactivará la descarga automática.
- Las reglas estrechas pueden rechazar rutas legítimas nuevas (p. ej. nombres con espacios o `?sequence=1`): el error es explícito y la ampliación requiere evidencia y pruebas.
- Un informe sin fecha de publicación en Crossref no aparecería en ninguna ventana anual; todos los informes revisados la tienen, pero es un riesgo residual que se vigilará comparando con la portada del histórico.
- **Alternativas descartadas:** API REST/OAI de DSpace (prohibidas por `robots.txt`), navegador automatizado (se usaría para superar el antibot), URL fijas de 12 informes (no detectan publicaciones nuevas).
