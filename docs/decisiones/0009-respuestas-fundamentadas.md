# ADR 0009 — Respuestas fundamentadas con Qwen local y verificación de citas y cifras

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F6A, issue #7).

## Decisión

- **Generador** (`generation.py`): Ollama local con `qwen3:4b-instruct` (`--modelo` o `BANREP_GENERATION_MODEL`), temperatura 0, semilla 0, contexto de 8 192 tokens y como máximo 600 tokens de salida. La conexión tiene un límite de 5 s y la respuesta, 180 s. Nunca descarga modelos: si falta, indica el comando `ollama pull`. Es independiente del índice: cambiarlo no exige reindexar.
- **Salida JSON:** se pide `format: "json"` con las claves `answer`, `citations` e `insufficient_evidence`; si la salida no es JSON, se reintenta una vez con la gramática del esquema. Con Ollama 0.33.2 la gramática del esquema dejó a Qwen sin salida hasta agotar los tokens en 2 de 14 consultas con cuadros, y `json` falló en otra que la gramática resolvía. El esquema se valida siempre después.
- **Separación de roles:** las instrucciones van en el mensaje de sistema; la pregunta y la evidencia, en el de usuario. Cada fragmento va en un bloque `<evidencia id="E#" informe periodo paginas_pdf tipo calidad>` y sus `<` y `>` se sustituyen por `‹` y `›`, de modo que un fragmento no puede cerrar su bloque ni abrir otro. El prompt declara la evidencia como datos no confiables y prohíbe obedecer sus órdenes.
- **Reglas al modelo:** responder solo con la evidencia; citar `[E#]`; copiar las cifras sin calcular, redondear ni convertir; distinguir proyección de dato observado y decir el periodo; no atribuir cifras de cuadros sin estructura (`table_raw`); abstenerse (`insufficient_evidence`) sin afirmar que el dato no existe en el Banco.
- **Verificación posterior** (`answering.verify`); si falla, la respuesta del modelo **no se muestra**:
  - formato y tipos del JSON;
  - citas existentes y al menos una;
  - cada cifra de la respuesta debe aparecer en el contenido de algún bloque citado: texto, notas del cuadro y solo los años del título y del periodo. Así «2025-10» no habilita la cifra «10».
    - De la pregunta solo se admiten años: una cifra de la premisa («¿fue 15 %?») no se confirma sin evidencia.
    - El signo cuenta: «-2,5» exige «-2,5»; en cambio, «0,4» se admite si la evidencia dice «-0,4».
    - Las cifras escritas en palabras como dato («diez por ciento», «dos puntos») se verifican igual.
    - Se detectan cifras pegadas a letras o a otras cifras («COP4.200», «5,1 %,9 %»).
    - Son equivalentes «4,7»/«4.7», «5,10»/«5,1» y «1.000»/«1000», y «2023» equivale a un encabezado «dic-23». Las cifras de fragmentos `unattributed_values` (cuadros sin estructura, ejes) no cuentan.
  - las páginas se admiten solo tras «pág.»/«página» y si pertenecen a un bloque citado. Un número de página no habilita una cifra igual.
  - las citas se reconocen sueltas o agrupadas («[E1, E2]»).
- **Abstención previa, sin llamar al modelo:** si no hay fragmentos, o si la mejor similitud semántica es menor que el umbral calibrado para el modelo de embeddings (`embeddinggemma`: 0,45). Calibración: preguntas con respuesta ≥ 0,512; fuera de tema (bitcoin, Ecopetrol, fútbol, recetas, clima) ≤ 0,363; «¿Qué es la inflación?» 0,475. Para modelos sin calibrar no hay umbral previo y decide Qwen con la evidencia.
- **Informes nombrados en la pregunta:** «informe de julio de 2025» o «IPM octubre 2025», y tras ellos «el de octubre de 2025», filtran la recuperación a esos informes si están indexados y no hay filtros explícitos. Un informe nombrado que no está indexado genera un aviso. Solo cuentan meses de publicación (enero, abril, julio, octubre) en formulaciones que nombran el informe. «En julio de 2025» o «para diciembre de 2025» pueden ser meses de datos u horizontes y no se interpretan. La detección se muestra en la salida.
- **CLI:** `consultar "pregunta"` con los mismos filtros que `buscar`, `--modo` (por defecto `auto`, que degrada a léxico si Ollama no está disponible para embeddings), `--top-k` (6), `--modelo` y `--json`. La salida incluye el estado (`answered`, `insufficient_evidence`, `rejected`), las fuentes con página física e impresa y URL, avisos del corpus y la declaración de proyecto no oficial.

## Evaluación con Qwen real (19-10-2026)

`qwen3:4b-instruct` y `embeddinggemma` en una GTX 1660 SUPER (ambos caben: 4,7 GB de VRAM). Las 17 consultas de `evaluacion/consultas_recuperacion.json`, recuperación híbrida (6 fragmentos). Medición con un script ad hoc; la evaluación automática de respuestas se incorporará a `evaluar` en F9.

| Medida | Resultado |
|---|---|
| Consultas con respuesta: respondidas y verificadas | 13 / 14 (1 rechazada: cifra «22» no presente en la evidencia citada) |
| Consultas sin respuesta: abstención | 3 / 3 (2 por umbral previo, 1 por el modelo) |
| Citas en una página juzgada relevante | 21 / 31 (cota inferior: los juicios no enumeran todas las páginas útiles) |
| Latencia de extremo a extremo | p50 3,0 s y p95 5,4 s con los modelos cargados; p50 14 s y p95 30 s en otra corrida con recarga de modelos en la GPU (17 muestras cada una) |

Las cifras no cambiaron tras endurecer la verificación por la revisión independiente: 13/14 respondidas, abstención 3/3 y 21/32 citas en página juzgada.

Pruebas con Qwen real (opcional, `BANREP_OLLAMA_LIVE=1`): responde «4,7 %» citando el bloque correcto y no repite la cifra inyectada («10 %») por un bloque que ordena ignorar las reglas.

## Límites conocidos

- **La verificación de cifras comprueba presencia, no significado.** Una cifra que existe en el bloque citado puede atribuirse a otra variable u horizonte. En la revisión manual, una respuesta sobre el salario mínimo usó «8» y «16» de un contexto que no los respaldaba como efectos sobre la inflación. Tras excluir las cifras de cuadros sin estructura ya no aparecen, pero el riesgo general persiste con enteros pequeños. Las comparaciones numéricas (F6B) no dependerán del modelo: se calcularán en Python desde celdas atribuidas.
- Si el modelo obedece una inyección y copia una cifra que **sí** está en el bloque inyectado, citándolo, la verificación no lo detecta. La cita queda visible y el bloque conserva su informe y su página.
- Respuestas sin cita dentro del texto pero con `citations` válidas se aceptan; la cita aparece en las fuentes.
- El umbral de abstención tiene poco margen: preguntas genéricas dentro del tema («¿Qué decidió la Junta Directiva?», 0,456) quedan cerca de 0,45, y una pregunta vaga («¿Cuál es la tasa?», 0,381) se abstiene pidiendo más precisión. Sin vectores (modo léxico o degradado) no hay umbral previo y decide el modelo.
- Corpus de cinco informes importados; sin memoria de conversación.
