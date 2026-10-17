# ADR 0006 — Procesamiento de PDF: bloques, cuadros y fragmentos citables

**Fecha:** 16 de octubre de 2026. **Estado:** aceptada (F4).

## Decisión

- **pdfminer.six** (MIT) para el análisis de diseño: cajas y líneas con coordenadas, tamaño de letra y negrilla. **pypdf** (BSD-3) sigue validando descargas e identificando importaciones. No se usa PyMuPDF (AGPL v3 o licencia comercial).
- Se descartó `pdfplumber`: los cuadros del Banco solo tienen filetes horizontales y su detector solo recuperaba encabezado y total; además era unas 5 veces más lento que pypdf. El texto de pypdf mezcla la barra lateral (gráficos y cuadros) con el texto principal y no permite reconstruir columnas.
- Cada página se divide en columnas por un canal vertical sin texto (sin contar títulos ni cabeceras). Los bloques se clasifican en título (con nivel), párrafo, leyenda («Gráfico/Cuadro/Recuadro…»), nota de fuentes, nota al pie (marcador unido a su texto), cabecera repetida, número de página impreso, índice, interior de gráfico y celda de cuadro.
- **Gráficos:** su interior (ejes, leyendas en letra de 5–6 pt) está fuera de alcance; se conserva una unidad con título, subtítulos de panel y fuentes, y la aclaración de que los datos no se extraen. Nada se infiere del gráfico.
- **Cuadros:** se reconstruyen por **líneas**: filas por línea base de las cifras; columnas por sus centros; encabezados por niveles (un nivel con varias celdas agrupa columnas, como años sobre trimestres, repartidos por programación dinámica en bloques contiguos); etiquetas de varias líneas; filas de grupo («Internas > Precios > …»); subtablas apiladas con encabezados propios; encabezados fusionados por pdfminer («dic 24 mar 25»); llamadas a nota («5,47 c/»); «n. d.» y «n. a.». Cuando la nota dice que **la negrilla indica proyección o supuesto**, cada cifra en negrilla se marca así. Ante cualquier ambigüedad (dos cifras en una celda, columna sin encabezado, fila sin etiqueta, texto suelto) el cuadro **no** se reconstruye: su texto se guarda por renglones como `table_raw`, sin atribuir cifras a columnas, y el fallo se cuenta en el informe de calidad.
- **Ligaduras:** algunos PDF declaran los glifos «fi»/«fl» como «f» en su ToUnicode («Gráfco», «infación»). Se detectan por su anchura normalizada frente a la «f» habitual de la misma fuente (en Fira Sans: «f» ≈ 0,332; «fi» ≈ 0,572; «fl» ≈ 0,590). Con dos grupos, el estrecho es «fi» y el ancho «fl»; con un solo grupo no se repara y se cuenta como no resuelto.
- **Unidades y fragmentos:** cada unidad guarda la página del PDF (1-indexada, la que sirve para `#page=N`), la página impresa y la ruta de secciones; los párrafos que siguen en la página siguiente se unen y conservan el rango. Los fragmentos (`max_chars` = 1 400) nunca mezclan informes ni secciones, se cortan en fin de oración y los de cuadros repiten título, columnas y notas (recortadas a 400 caracteres). `chunk_id` depende del informe, el hash del PDF, las versiones del procesamiento y del fragmentador, la posición y el texto.
- **Aislamiento:** cada PDF se analiza en un proceso hijo con 15 minutos y 3 GB como máximo (identificación de importaciones: 60 s y 1 GB). Un PDF hostil no bloquea ni agota el equipo.
- **Caché:** `processed/<informe>-<hash>-<versión>.json` con unidades, informe de calidad, fragmentos y metadatos del snapshot; la versión incluye la de pdfminer. Mismo PDF y mismas versiones = sin trabajo; se verifica el SHA-256 del PDF antes de procesarlo, un JSON dañado se reprocesa, los metadatos del snapshot se actualizan sin reprocesar y un informe que falla no impide guardar los demás.
- **Informe de calidad:** páginas con texto, caracteres, unidades por tipo, bloques excluidos por tipo, cuadros reconstruidos y fallidos (con motivo), ligaduras reparadas y sin resolver. Un documento se marca **degradado** si menos del 80 % de las páginas tienen texto, si tiene muy poco texto, más de 50 ligaduras sin resolver o más de la mitad de los cuadros sin reconstruir.

## Evidencia (5 PDF reales, enero de 2025 a enero de 2026; `banrep-rag procesar`, 3 procesos)

| Informe | Págs. | Fragmentos | Cuadros reconstruidos | Sin reconstruir (texto conservado) | Ligaduras reparadas | Calidad |
|---|---|---|---|---|---|---|
| 2026-01 | 54 | 388 | 7 | 2 (regiones sin leyenda, pp. 41–42) | 0 | aceptable |
| 2025-10 | 51 | 389 | 7 | 0 | 0 | aceptable |
| 2025-07 | 54 | 388 | 7 | 0 | 0 | aceptable |
| 2025-04 | 56 | 327 | 6 | 1 (anexo trimestral, continuación) | 593 (3 sin resolver) | aceptable |
| 2025-01 | 61 | 359 | 6 | 8 (recuadros y anexo trimestral) | 650 (3 sin resolver) | degradado |

**Verificación independiente con pypdf:** cada fila de cada cuadro reconstruido se busca en el texto de pypdf con la misma etiqueta y las cifras en el mismo orden (o con paso constante, porque pypdf a veces lee los anexos por columnas): 236/236 (julio y octubre de 2025), 239/239 (enero de 2026) y 170/172 (enero y abril de 2025; las 2 restantes coinciden con las mismas filas históricas de los otros informes). Cobertura de palabras de pypdf en las unidades: 0,94–0,97; lo ausente son ejes y leyendas de gráficos, fragmentos de palabras partidas por guion que aquí se unen y las formas rotas de ligaduras que aquí se reparan. En los cinco se reconstruyen el Cuadro 2.1, el Cuadro 3.1, el Anexo 1 (dos subtablas) y el Anexo 2 (anual y trimestral; su página de continuación sin título se conserva como «(continuación)»). Tiempo total: unos 30 s en un equipo de 12 núcleos.

**Revisión independiente:** encontró que la página de continuación del anexo trimestral se perdía en silencio (corregido: una región sin leyenda nunca se descarta), recuadros sin nombre en la ruta de secciones, notas de cuadros recortadas sin otro fragmento (ahora `table_notes`), procesamiento todo o nada (ahora por informe), ligaduras sin contar en fuentes donde la ligadura es más frecuente que la «f» (ahora la referencia es la «f» más estrecha) y orden de lectura del resumen (ahora por altura, intercalando columnas). Una segunda ronda corrigió las subsecciones «x.y» de los recuadros (continuidad numérica; al salir se restaura la sección del texto principal) y las notas pequeñas («Significancia…», «* …») que se excluían como rótulos.

## Límites y riesgos

- Las reglas se ajustaron a la maquetación actual del Banco (2025–2026). Informes más antiguos o un cambio de plantilla pueden reducir la tasa de cuadros reconstruidos: el informe de calidad lo hará visible, pero no se corrige solo.
- La marca de proyección depende de que la nota del cuadro lo diga. La jerarquía de filas de grupo se infiere por posición.
- No hay OCR: un PDF escaneado queda degradado y sin texto.
- El análisis de diseño no conoce el significado económico: «proyección» o «pronóstico» en el texto no se separa de lo observado. Eso corresponde a la generación y a la comparación (F6).

**Alternativas descartadas:** PyMuPDF (licencia), pdfplumber (no recupera estas filas), extracción simple de texto (mezcla columnas y desordena cuadros), OCR (fuera de alcance y costoso).

## Actualización H2 — integridad de artefactos procesados

Antes de preparar un índice, `ProcessedStore.ensure` verifica el SHA-256 completo de **cada PDF**, incluso con un acierto de caché, y exige su ruta exacta `raw/<informe>-<sha>.pdf` sin enlaces simbólicos en `raw/` ni en el archivo. No se repetirá este hash en cada consulta. La firma de ruta incorpora `cache=3`: los JSON de versiones anteriores permanecen en disco y se reconstruyen bajo nombres nuevos, sin migrar silenciosamente datos de una versión no comprobada.

La versión nueva guarda SHA-256 del contenido serializado (`document`, `chunks`, `versions`); valida identidad del informe y PDF, versiones y configuración, cantidad/tipos de unidades, páginas 1-indexadas y contadores de calidad. Las advertencias y el estado degradado se deducen de los mismos contadores que en el procesamiento; los cuadros fallidos se asocian a unidades `table_raw`. Además, reconstruye **todos** los fragmentos desde las unidades y compara texto, páginas, etiquetas, sección e identificadores. Los metadatos `report`/`pdf` se toman del catálogo vigente y se pueden actualizar sin cambiar ese hash. Un JSON ilegible o incompatible se reconstruye con aviso; una salida inválida o un PDF cambiado o desaparecido durante el trabajo **no sustituye** el artefacto anterior. El bloqueo `.processing.lock` serializa escritores de caché por instalación; el catálogo conserva su propio bloqueo.

**Límite de confianza:** el hash guardado junto al JSON detecta corrupción, pero no autentica modificaciones coordinadas del archivo local. Reconstruir los fragmentos impide falsificar citas respecto de las unidades almacenadas; **no demuestra** que esas unidades coincidan con los bytes del PDF sin volver a extraerlos. La carpeta de datos local debe protegerse contra escritura no autorizada.

Un PDF válido sin texto extraíble se conserva con cero unidades/fragmentos y calidad degradada; no aporta citas a un índice. Una página vacía entre dos extremos de un párrafo continuo tampoco se cuenta como página con texto.

**Evidencia:** copia temporal de los cinco PDF importados, sin llamadas de red: 1.851 fragmentos y calidad idéntica al baseline F4; primera pasada reconstruida, segunda pasada cinco aciertos de caché, verificados tras el endurecimiento de la calidad. Esto comprueba procesamiento de documentos reales, **no** fidelidad económica completa ni N=12. La asociación de `table_raw` con cifras/horizontes y el exceso de longitud de una fila siguen en H4/H5 antes de indexar.
