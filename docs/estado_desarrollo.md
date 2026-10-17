# Estado del desarrollo — checkpoint F4

**Fecha:** 16 de octubre de 2026. **`main` verificada al retomar:** `2175077556b56c8ea6a689c1565485268288b69d`; CI aprobado, árbol limpio, sin PR abiertos. **Próxima fase:** endurecimiento H1–H5 antes de activar F5.

## Resumen por fase

| Fase | Issue | Estado | Evidencia |
|---|---|---|---|
| F0 Auditoría | #1 | cerrada | ADR 0001, diagnóstico inicial |
| F1 Base | #2 | cerrada | CI `Pruebas y calidad`, `uv.lock`, pre-commit |
| F2 Descubrimiento | #3 | **descubrimiento real verificado** (Crossref, N=12) | PR #20; prueba en vivo y `banrep-rag descubrir` desde Ubuntu y GitHub Actions |
| F3 Descarga y sincronización | #4 | implementada; **descarga real de 1 PDF verificada en GitHub Actions**; N=12 real pendiente | PR #14–#17, #19, #22; ejecución 38936215976 |
| Sprint de acceso | #18 | casi completo (falta N=12 real y repetición) | tabla de evidencia en el issue |
| F4 Procesamiento | #5 | **completada** (con límites documentados) | PR #24, #25; ADR 0006; verificación con pypdf |
| F5–F10 | #6–#11 | no iniciadas | — |

## Qué funciona hoy (comandos reales)

- `banrep-rag descubrir` — 12 informes principales en español desde Crossref (julio 2026 → octubre 2023), con DOI, fecha de publicación y URL institucional.
- `banrep-rag sincronizar [--dry-run] [--revalidar-dias] [--pausa] [--usar-importados]` — descarga verificada, incremental, con snapshots y activación atómica. Desde Ubuntu el repositorio suele responder con reto Radware tras pocas solicitudes; desde GitHub Actions la descarga funcionó.
- `banrep-rag importar … [--activar] [--sin-red] [--reemplazar]` — importación de PDF obtenidos legítimamente, identificados con dos evidencias y verificados en Crossref.
- `banrep-rag procesar` — unidades y fragmentos citables con informe de calidad (5 PDF reales en ~30 s). Cuadros verificados con pypdf: 236/236, 236/236, 239/239 y 170/172 (+2 confirmadas entre informes).
- `banrep-rag estado`, `banrep-rag diagnostico-fuentes` y el workflow manual/semanal «Diagnóstico de fuentes».

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

## Datos locales del equipo de desarrollo (no versionados)

- `data/catalogo/`: catálogo activo de **importación local** con los 5 PDF del repositorio original (enero, abril, julio y octubre de 2025; enero de 2026), metadatos verificados en Crossref, y `processed/` con sus fragmentos.
- No es la ventana de los últimos 12: faltan julio, abril (2026) y los informes de 2023–2024.

## Decisiones clave

ADR 0001 (aislamiento del repo original) · 0002 (pypdf para validar) · 0003 (snapshots, sincronización incremental) · 0004 (fuentes: Crossref, no DSpace REST, no eludir Radware, política de URL) · 0005 (importación local) · 0006 (procesamiento con pdfminer, cuadros, ligaduras, fragmentos).

## Pendiente y riesgos conocidos

1. **D4:** sincronización real N=12 y repetición sin descargas (Ubuntu con pausas amplias o runner de GitHub sin guardar PDF).
2. **F5:** embeddings locales (Ollama: `embeddinggemma` y `bge-m3` instalados), FAISS + léxico (SQLite FTS5), fusión RRF, manifiesto de índice activo ligado al snapshot y a las versiones de procesamiento; reranker opcional.
3. F4: en enero de 2025 no se reconstruyen 8 cuadros (recuadros y página trimestral del anexo; texto conservado sin columnas, informe marcado como degradado); en abril de 2025, la página trimestral del anexo. Heurísticas ajustadas a la plantilla 2025–2026; 3 ligaduras sin resolver en enero y abril de 2025 (títulos).
4. Acciones de GitHub fijadas por etiqueta (revisar en F9). Windows no probado.
5. El detector programado de publicaciones (F8) aún no existe; el workflow de diagnóstico no alerta.

## Reanudación: integridad anterior al índice

- Baseline local: `uv sync --frozen`; 328 pruebas aprobadas, 1 omitida (red real); Ruff y Mypy aprobados. El catálogo local sigue siendo una **importación de cinco PDF**, no N=12. Ollama enumera `embeddinggemma:latest`, `bge-m3:latest` y `qwen3:4b-instruct`: su presencia no demuestra ejecución de modelos.
- H1 (#27) fusionada mediante PR #32 a `main` `34c3786b6bf2eab2dd08744926dbb8b74a1268b9`; CI post-merge aprobado (run 38999188560). Revisión independiente: dos fallos medios de arranque/cola corregidos, sin altos/medios restantes. 339 pruebas offline aprobadas, 1 omitida. Windows/macOS y descarga real siguen sin verificar.
- H2 (#28), rama `fix/cache-procesamiento-segura`: firma de caché nueva, verificación del PDF incluso en aciertos, ruta segura, fragmentos reconstruidos desde unidades, calidad coherente, bloqueo de escritores y reconstrucción controlada. La revisión independiente final no encontró fallos altos ni medios; **CI remoto y fusión pendientes**. En copia temporal de los cinco PDF reales se produjeron 1.851 fragmentos con los conteos F4; segunda pasada: cinco aciertos tras las últimas correcciones. No son los 12 informes más recientes ni se probó una descarga. Un SHA guardado en el JSON no autentica las unidades contra el PDF.
- H3 (#29): persistir resultado fallido de revalidaciones sin fingir frescura. H4 (#30) y H5 (#31): evidencia por unidad, `table_raw`, notas y límites del fragmentador. No publicar índice hasta cumplir las políticas críticas.
- F5 (#6) sigue sin índice; F6–F10 permanecen pendientes. La próxima unidad prioritaria es H3 y las políticas seguras H4/H5, después recuperación léxica e índice transaccional.

## Cómo retomar

```sh
git switch main && git pull --ff-only
uv sync --frozen && uv run --frozen pytest -q
uv run --frozen banrep-rag estado --data-dir data/catalogo
```
Leer `CLAUDE.md` y las ADR; continuar con el issue #6 (F5).
