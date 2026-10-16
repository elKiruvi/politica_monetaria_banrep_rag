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
- **Caché:** `processed/<informe>-<hash>-<versión>.json` con unidades, informe de calidad, fragmentos y metadatos del snapshot. Mismo PDF y mismas versiones = sin trabajo.
- **Informe de calidad:** páginas con texto, caracteres, unidades por tipo, bloques excluidos por tipo, cuadros reconstruidos y fallidos (con motivo), ligaduras reparadas y sin resolver. Un documento se marca **degradado** si menos del 80 % de las páginas tienen texto, si tiene muy poco texto, más de 50 ligaduras sin resolver o más de la mitad de los cuadros sin reconstruir.

## Evidencia (5 PDF reales, enero de 2025 a enero de 2026; `banrep-rag procesar`, 3 procesos)

| Informe | Págs. | Fragmentos | Cuadros reconstruidos | Sin reconstruir | Ligaduras reparadas | Calidad |
|---|---|---|---|---|---|---|
| 2026-01 | 54 | 387 | 6 | 0 | 0 | aceptable |
| 2025-10 | 51 | 409 | 6 | 0 | 0 | aceptable |
| 2025-07 | 54 | 404 | 6 | 0 | 0 | aceptable |
| 2025-04 | 56 | 362 | 6 | 0 | 490 | aceptable |
| 2025-01 | 61 | 576 | 6 | 6–7 (recuadros) | 486 | degradado |

En los cinco se reconstruyen el Cuadro 2.1, el Cuadro 3.1, el Anexo 1 (dos subtablas) y el Anexo 2 (anual y trimestral). Los fallos de enero de 2025 son cuadros de recuadros (textuales, kernel, literatura): quedan como texto sin columnas. Tiempo total: unos 31 s en un equipo de 12 núcleos.

## Límites y riesgos

- Las reglas se ajustaron a la maquetación actual del Banco (2025–2026). Informes más antiguos o un cambio de plantilla pueden reducir la tasa de cuadros reconstruidos: el informe de calidad lo hará visible, pero no se corrige solo.
- La marca de proyección depende de que la nota del cuadro lo diga. La jerarquía de filas de grupo se infiere por posición.
- No hay OCR: un PDF escaneado queda degradado y sin texto.
- El análisis de diseño no conoce el significado económico: «proyección» o «pronóstico» en el texto no se separa de lo observado. Eso corresponde a la generación y a la comparación (F6).

**Alternativas descartadas:** PyMuPDF (licencia), pdfplumber (no recupera estas filas), extracción simple de texto (mezcla columnas y desordena cuadros), OCR (fuera de alcance y costoso).
