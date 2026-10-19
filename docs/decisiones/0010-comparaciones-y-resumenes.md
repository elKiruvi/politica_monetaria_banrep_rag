# ADR 0010 — Comparaciones deterministas entre informes y resúmenes trazables

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F6B, issue #7).

## Decisión

### Comparaciones (`comparison.py`, `banrep-rag comparar`)

El modelo de lenguaje **no** interviene en las cifras. El procedimiento es explícito:

1. **Informes.** Se toman de `--informe` (ipm-AAAA-MM o AAAA-MM, repetible) o de la pregunta, por mes de publicación y año («entre enero y julio de 2025», «enero 2025», «2025-01»). Un mes sin año toma el siguiente año explícito. Si se pide un informe que no está indexado, se informa; con menos de dos informes no se compara (código 2).
2. **Evidencia por informe.** Se recorren **todos** los fragmentos `table` con `structured_cells` de cada informe, no un top-k global que pudiera estar dominado por un solo documento. Los `table_raw` y los gráficos no aportan cifras.
3. **Variables.** De cada `attributed_cell` se toman el cuadro (título sin «(continuación)», «(Años)», partes ni llamadas de nota), la fila (sin llamadas de nota pegadas, como «IPC totald/» → «IPC total»), la columna u horizonte, la unidad, el valor textual y la marca de proyección. El valor se interpreta como decimal español («4,7», «-13.451», «4,80 c/»); «n. d.» y «n. a.» se descartan.
4. **Indicador.** Un `--indicador` explícito se usa completo, de modo que «tasa de cambio» conserva «cambio». De una pregunta se quitan las palabras de función («desde», «hasta», «frente», «pronóstico»…) y los periodos. El límite es de 500 caracteres y 6 términos. Los sinónimos se ordenan de lo más específico a lo más general («inflación» → «ipc total», «ipc», «inflación»; «crecimiento»/«PIB» → «producto interno bruto»). Se usa la primera combinación que coincide con alguna fila. La interpretación se muestra siempre, también cuando no hay coincidencias.
5. **Comparabilidad.** Solo se comparan celdas con el mismo cuadro, fila y columna (horizonte exacto: «2025» y «dic-25» son distintos) y la misma unidad. Si la unidad difiere, la fila se marca como no comparable y no se calcula nada. Si un mismo informe trae valores distintos para la misma ruta (filas homónimas con un grupo padre no identificado por F4, como «Porcentaje del PIB» en la balanza de pagos), la fila se marca como **ambigua** y tampoco se compara. El Anexo 1 (analistas) y el Anexo 2 (pronóstico del Banco) nunca se mezclan.
6. **Cálculo.** Las diferencias entre informes consecutivos se calculan con `Decimal`, con la precisión de la cifra más precisa. Con unidad porcentual se expresan en puntos porcentuales; sin unidad declarada, se dice así. Cada diferencia se etiqueta como revisión de proyección, proyección frente a dato observado, dato observado frente a proyección o cifras sin marca de proyección. Si los informes publican con distinta precisión (4,05 frente a 4,7), se advierte que parte de la diferencia puede ser redondeo.
7. **Citas.** Cada valor lleva su informe, página física e impresa y URL, y las notas del cuadro se recuperan por `table_id`. Cuenta como proyección la negrilla que el cuadro declara o «proy» en el encabezado de la columna (Cuadro 2.1: «2025 (proy)»). Por defecto se omiten las filas sin ninguna cifra marcada como proyección, se dice cuántas y `--incluir-observados` las muestra. La salida se limita a 40 filas.

### Resúmenes (`banrep-rag resumir`)

Reutilizan el flujo verificado de `consultar` (ADR 0009), restringido a un informe. Sin `--tema` se prioriza la sección «Resumen»; con `--tema`, la evidencia del tema dentro del informe. Un resumen mezcla cifras de muchos bloques y el modelo suele citar solo algunos. Si una cifra aparece en un bloque revisado pero no citado, ese bloque **se agrega a las citas** como «fuente añadida por la verificación» (`scope="all"`), de modo que toda cifra tenga una fuente visible. Una cifra que no está en ningún bloque se rechaza. En `consultar` las cifras se verifican solo contra los bloques citados. La revisión independiente detectó que la primera versión aceptaba cifras de bloques no citados sin mostrarlos; se corrigió así.

## Resultados reales (cinco informes importados, 19-10-2026)

- «¿Cómo cambiaron las proyecciones de inflación entre enero y julio de 2025?»:
  - IPC total, horizonte 2025: 4,05 % (enero, pág. 57) → 4,7 % (julio, pág. 49), +0,65 pp, revisión de proyección, con aviso de redondeo.
  - Horizonte 2026: 3,05 % → 3,2 %, +0,15 pp.
- Octubre de 2025 → enero de 2026, IPC total 2026: 3,6 % → 6,3 %, +2,7 pp.
- Producto interno bruto (octubre de 2025 → enero de 2026): +0,3 pp (2025) y −0,3 pp (2026).
- Resúmenes verificados de octubre de 2025 y de enero de 2026 (tema «mercado laboral»), en unos 20 s cada uno. En julio de 2025 (tema «mercado laboral»), el bloque del Anexo 2 que respalda «9,2 %» se agregó como fuente.
- Cuadro 2.1, Estados Unidos (enero → julio de 2025, horizonte «2025 (proy)»): −0,7 (unidad no declarada en la celda).

## Límites

- La coincidencia de filas es léxica con sinónimos; un indicador ambiguo puede abarcar varias filas (se listan todas) o ninguna. `--indicador` permite precisarlo.
- La marca de proyección proviene de la negrilla según la nota del cuadro (F4). Un cuadro que no marca proyecciones aparece como «sin marca», no como observado.
- La redacción narrativa de una comparación con Qwen no se implementa: la tabla calculada ya es la respuesta. Se podrá añadir con la misma verificación de cifras.
- En los resúmenes, la verificación de presencia de cifras no garantiza su interpretación. Por ejemplo, la mediana esperada por los analistas puede presentarse como dato.
