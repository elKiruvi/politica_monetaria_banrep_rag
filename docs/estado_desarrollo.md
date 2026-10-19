# Estado del desarrollo — checkpoint F8 + adquisición asistida

**Actualizado:** 19 de octubre de 2026. H1–H5 fusionadas (#27–#31 cerrados). **F5 cerrada** (#6): PR #37 y #38, `main` `763b655`, CI post-merge aprobado (run 39202051557). **F6 cerrada** (#7): PR #39 y #40, `main` `33fb27f`, CI post-merge aprobado (run 39222532131). **F7 cerrada** (#8): PR #41, #42 y #43, `main` `0292438`, CI post-merge aprobado (run 39260908249). **F8 cerrada** (#9): PR #44 (`main` `6f6a214`, CI post-merge aprobado, run 39265452849) y PR #45 (Docker, `main` `bc37162`, CI post-merge aprobado). **Adquisición asistida** (F3, ADR 0015): freno tras un bloqueo antibot, `faltantes`, `importar CARPETA` y `sincronizar --sin-descargar`. **Siguiente fase:** F9 (#10).

## Resumen por fase

| Fase | Issue | Estado | Evidencia |
|---|---|---|---|
| F0 Auditoría | #1 | cerrada | ADR 0001 |
| F1 Base | #2 | cerrada | CI `Pruebas y calidad`, `uv.lock`, pre-commit |
| F2 Descubrimiento | #3 | **verificado con red real** (Crossref, N=12) | PR #20 |
| F3 Descarga y sincronización | #4 | implementada; 1 PDF descargado en GitHub Actions; **N=12 real pendiente** (Radware desde Ubuntu); adquisición asistida con navegador e importación (ADR 0015) | PR #14–#19, #22; run 38936215976 |
| Sprint de acceso | #18 | falta N=12 real y repetición | tabla del issue |
| F4 Procesamiento | #5 | **completada**; H1–H5 fusionadas (PR #32–#36) | ADR 0006 |
| F5 Indexación y recuperación | #6 | **cerrada**: FTS5, FAISS, RRF híbrida, manifiesto, reversión, evaluación | ADR 0007, 0008 |
| F6 Generación y comparación | #7 | **cerrada**: `consultar` verificado, `comparar` determinista, `resumir` trazable | ADR 0009, 0010 |
| F7 Producto | #8 | **cerrada**: servicio, API, web y reportes (CLI, API y web) | ADR 0011, 0012 |
| F8 Automatización | #9 | **Cerrada**: detector Crossref (workflow), `actualizar`, Docker y Compose con prueba de humo en Actions | ADR 0013 y 0014 |
| F9–F10 | #10–#11 | pendientes | — |

## Qué funciona hoy (comandos reales)

- `descubrir`, `sincronizar`, `importar`, `estado`, `diagnostico-fuentes` (F2–F3, ver README).
- `procesar`: cinco PDF importados → 1 566 fragmentos (304, 304, 303, 311 y 344; `max_chars=1400`), cuadros con calidad por unidad; enero de 2025 degradado.
- `indexar`, `buscar`, `validar-indice`, `evaluar`: índice léxico en < 1 s y vectores `embeddinggemma` en ~80 s (GPU). Nivel evidencia, k=5: léxica MRR 0,562; híbrida 0,687, R@5 0,714 (ADR 0007, 0008).
- `consultar`: respuestas con Qwen verificadas; 13/14 respondidas, abstención 3/3, p50 3 s (ADR 0009).
- `comparar` y `resumir`: IPC total 2025, enero → julio de 2025: 4,05 % → 4,7 % (+0,65 pp, revisión de proyección) (ADR 0010).
- `servir` (PR #41): API `/api/v1` local con las mismas operaciones; prueba de humo real con Qwen y embeddinggemma (ADR 0011).

## Evidencia de acceso (16-10-2026)

| Prueba | Ubuntu (usuario) | GitHub Actions |
|---|---|---|
| Crossref | 200, 12 informes | 200 |
| Portada del histórico | 200 | 200 |
| Histórico `?page=1` | reto Radware | reto Radware |
| DOI → ruta heredada | 302 | 302 |
| Ruta heredada → `/server/.../content` | 302 una vez; luego reto Radware por tasa | 302 |
| Descarga de 1 PDF (julio 2026) | bloqueada | **verificada**: 49 págs., 5 114 903 bytes, SHA-256 `27bec154…40b717` |
| Sincronización N=2 / N=12 real | bloqueada: reto Radware en `/server/.../content` desde la primera descarga | pendiente |

## Estado de la descarga (19-10-2026)

- **Diagnóstico** (`diagnostico-fuentes`, desde Ubuntu):
  - Responden bien Crossref (200, último informe 2026-07), la portada del histórico y `robots.txt` (200).
  - El DOI redirige a la ruta heredada, y esta a `/server/api/core/bitstreams/<uuid>/content` (302 en ambos casos).
  - La paginación del histórico recibe un reto Radware. Minutos después, también lo recibieron la portada y la página del informe.
  - `robots.txt` sigue con `Disallow: /server`.
  - La descarga automática desde este equipo no es viable sin eludir controles, y no se eluden.
- **Implementado** (ADR 0015):
  - Un reto detiene todas las descargas de la ejecución y bloquea nuevas solicitudes durante 12 h.
  - `banrep-rag faltantes` lista lo que falta con su enlace DOI.
  - `importar ~/Descargas` importa los IPM de una carpeta y omite los demás PDF.
  - `sincronizar --sin-descargar` activa la ventana de los últimos 12 sin pedir PDF.
- **Ventana real** (`faltantes` sobre `data/catalogo`, consultando Crossref):
  - Hay 5 de 12 importados: 2026-01, 2025-10, 2025-07, 2025-04 y 2025-01.
  - **Faltan 7:** 2026-07, 2026-04, 2024-10, 2024-07, 2024-04, 2024-01 y 2023-10. La persona debe descargarlos con su navegador y luego ejecutar `importar` y `sincronizar --sin-descargar`.
  - Los informes de 2023–2024 no se han validado en el procesamiento (F4); revisar `procesar` al importarlos.
- **Pendiente:** huella SHA-256 oficial observada desde Actions (sin publicar el PDF) para contrastar los importados. Revisar antes la frecuencia y `robots.txt`.

## Datos locales del equipo de desarrollo (no versionados)

- `data/catalogo/`: catálogo activo de **importación local** con 5 PDF (enero, abril, julio y octubre de 2025; enero de 2026), metadatos verificados en Crossref, `processed/` e `indexes/`. **No** es la ventana de los últimos 12.
- Ollama enumera `qwen3:4b-instruct`, `bge-m3`, `embeddinggemma` y `gemma3:4b`; no se ha medido su ejecución en este checkpoint.

## Pendiente y riesgos conocidos

### Trabajo en curso (19-10-2026)

- **F8, verificado:**
  - El workflow «Detector de publicaciones» se ejecutó a mano en `main` (run 39265774418, `workflow_dispatch`): estado `sin_cambios` y ningún issue abierto. La referencia versionada tiene 12 informes, de 2026-07 a 2023-10.
  - El workflow «Docker» construye la imagen y la arranca con Compose: *healthy*, UID 10001, sin PDF ni datos, `/ready` 503 sin índice, `Host` ajeno 400 y escritura en un catálogo 0700 con el UID de su dueño.
- **Docker en un equipo real:**
  - Ya está instalado Docker 29.9.1 *rootless* en Ubuntu 26.04 (sin demonio root ni grupo `docker`).
  - `docker compose up` está verificado con una copia del catálogo real y `BANREP_UID=0`: *healthy*, `/ready` 200, búsqueda léxica, `Host` ajeno 400, puerto solo en loopback.
  - Con `BANREP_UID=1000`, `/ready` da **500**. Mejorar el mensaje en F9: debería decir «no se puede leer el catálogo (permisos)».
  - **Falta** probar el perfil `ollama` (descarga la imagen y los modelos, varios GB: requiere decisión de la persona) y `ollama-gpu` (en modo *rootless* requiere el NVIDIA Container Toolkit sin cgroups).
- **Revisión visual de la web en un navegador:** sigue pendiente; en las sesiones de desarrollo no hubo herramienta de navegador. Abrir `http://127.0.0.1:8000` tras `banrep-rag servir`, probar cada pestaña con y sin Ollama, y descargar un reporte de cada formato.

### Fases siguientes

1. **F9, evaluación, seguridad y rendimiento (#10):**
   - Ampliar `evaluacion/` (más de 14 consultas, juicios sin pooling léxico, consultas sin respuesta).
   - Integrar en `evaluar` la evaluación automática de respuestas (hoy es un script aparte): fidelidad, citas, abstención y consistencia numérica.
   - Reevaluar el reranker y la calibración del umbral de abstención (0,45, poco margen).
   - Auditoría de dependencias (`uv audit` o similar), Gitleaks, permisos y fijación por SHA de las acciones de GitHub.
   - CI en Windows.
   - P50 y P95 de cada etapa y consumo de RAM y VRAM.
   - Fijar las imágenes base de Docker por *digest*.
2. **F10, auditoría final (#11):** clon limpio, revisión independiente completa, pruebas de extremo a extremo y documentación final, incluido el procesamiento de PDF dentro del contenedor (límite de 4 GB frente a los 3 GB del proceso aislado).

### Pendientes externos y limitaciones conocidas

- **F3, #4 y #18:** sincronización real de los últimos 12 informes y repetición sin descargas. Desde Ubuntu, Radware bloquea la descarga desde la primera solicitud (no se elude). El corpus actual son **cinco informes importados**, no los últimos 12.
- **F4:**
  - Enero de 2025 está degradado (8 cuadros como `table_raw`).
  - Las heurísticas están ajustadas a 2025–2026; los informes de 2023–2024 no se han validado.
  - En el Anexo 2, filas homónimas sin su grupo padre («Porcentaje del PIB») quedan ambiguas.
- **Generación:**
  - La verificación de cifras comprueba presencia, no interpretación: un resumen presentó la mediana de los analistas como dato.
  - Una inyección que cite su propio bloque no se detecta.
- **Detector:**
  - Un informe retirado dentro de la ventana no se detecta como tal.
  - Dos ejecuciones manuales casi simultáneas podrían duplicar un issue, por el retraso del índice de búsqueda de GitHub.
  - La actualización local depende de que el equipo esté encendido. Con `--continuar-si-falla`, un bloqueo de la fuente termina con código 3.
- **Producto:**
  - La API no tiene autenticación ni TLS y atiende la generación de una en una.
  - `/ready` es costosa: calcula el hash de los PDF.
- **Plataformas y CI:** Windows no se ha probado. Las acciones de GitHub están fijadas por etiqueta (revisar en F9).

## Cómo retomar

```sh
git switch main && git pull --ff-only
uv sync --frozen && uv run --frozen pytest -q
uv run --frozen banrep-rag estado --data-dir data/catalogo
uv run --frozen banrep-rag indexar --data-dir data/catalogo && uv run --frozen banrep-rag evaluar --data-dir data/catalogo
```
Leer `CLAUDE.md` y las ADR 0001–0014. Retomar por F9 (#10).
