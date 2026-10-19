# ADR 0011 — Servicio compartido y API HTTP local

**Fecha:** 19 de octubre de 2026. **Estado:** aceptada (F7A, issue #8).

## Decisión

- **Servicio** (`service.py`, `RagService`): una sola orquestación para la CLI, la API y la web.
  - Cubre apertura del índice, recuperación con degradación a léxico, consulta, comparación, resumen, estado del corpus y de Ollama, y lista de informes.
  - No imprime: devuelve resultados con avisos y lanza `ServiceError` con un tipo y con los avisos reunidos hasta el fallo; así el de privacidad se muestra aunque la operación falle. Los mensajes no exponen rutas locales: la carpeta de datos se muestra como `<datos>` y el directorio personal, como `~`. Cada interfaz lo traduce: `invalid` → código 2 / HTTP 422; `not_found` → 2 / 404; `unavailable` → 1 / 503.
  - Los proveedores (embeddings, generación, sonda de Ollama) son inyectables, para las pruebas y para futuros adaptadores.
- **API** (`api.py`, FastAPI, `/api/v1`):
  - **Liveness y readiness:** `GET health` solo dice que el proceso vive. `GET ready` responde 200 únicamente con un índice válido e informa también si Ollama y el modelo de generación están disponibles.
  - **Consulta de estado:** `GET status` y `GET reports`.
  - **Operaciones:** `POST search`, `query`, `compare` y `summarize`.
  - **Esquema OpenAPI:** en `/api/v1/openapi.json`. Swagger UI está desactivado: carga recursos de un CDN y la CSP lo bloquearía.
  - **Respuestas:** las que llevan contenido documental incluyen el aviso de proyecto no oficial; los `Decimal` se serializan como texto, sin pérdida de precisión.
- **Validación:** modelos Pydantic con `extra="forbid"`, longitudes máximas (pregunta ≤ 500, `top_k` ≤ 50 o 12), patrones de periodo e informe, y cuerpo ≤ 16 KB (413). El límite lo aplica un middleware ASGI que cuenta los bytes recibidos, también con `Transfer-Encoding: chunked`. Un JSON mal formado recibe «El cuerpo de la solicitud no es JSON válido». Los errores de validación se resumen en español («Solicitud inválida en: …»), sin trazas; un error inesperado responde 500 genérico.
- **Seguridad por defecto:**
  - `banrep-rag servir` escucha en 127.0.0.1 (o `localhost`, `::1`) y se niega a usar otra dirección sin `--permitir-red`, porque no hay autenticación. Con `--permitir-red` se aceptan todos los `Host`, lo que **desactiva la protección contra DNS rebinding**: úselo solo detrás de un proxy con autenticación.
  - `TrustedHostMiddleware` acepta solo hosts locales, lo que protege contra *DNS rebinding* desde un sitio web malicioso.
  - Sin CORS, de modo que otros orígenes no pueden leer respuestas.
  - Cabeceras: `Content-Security-Policy` restrictiva, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` y `no-store`, en todas las respuestas, también en las de error 413 y 500.
- **Privacidad:** el aviso «Ollama no es local» se emite una sola vez y solo cuando una operación contacta a Ollama; la búsqueda léxica no lo emite.
- **Contexto del modelo:** la evidencia enviada a Qwen tiene un presupuesto de 16 000 caracteres (ADR 0009). Los bloques entran por relevancia, nunca se recortan a medias, y uno omitido no puede citarse. Antes, una consulta léxica podía exceder el contexto (10 619 de 8 192 tokens).
- **Concurrencia:** las llamadas al modelo de lenguaje se serializan con un candado (la GPU de referencia atiende una a la vez). Si está ocupado más de 1 s, se responde 503 «reintente». La búsqueda y la comparación no usan el candado.

## Verificación

- Pruebas con `TestClient` sobre un catálogo real de prueba:
  - Estado sin índice y con índice; búsqueda, consulta, rechazo y fallo del generador.
  - Errores tipados, validaciones, filtros desconocidos, cabeceras, `Host` ajeno, cuerpo grande.
  - Serialización concurrente, errores sin filtrar rutas, y `servir` que rechaza 0.0.0.0.
- Prueba de humo real con `servir` sobre los cinco informes importados, Qwen y embeddinggemma:
  - `/ready` 200 con 1 566 fragmentos.
  - `/query` responde con citas (octubre de 2025, petróleo, págs. 13 y 19).
  - `/compare` da +0,65 pp; con `Host: evil.com`, 400.

## Revisión independiente

No encontró hallazgos altos. Halló 4 medios, todos corregidos:
- límite del cuerpo eludible con `chunked`;
- rutas absolutas en errores;
- `/docs` en blanco por la CSP;
- avisos perdidos al fallar.

Corregidos también varios bajos: aviso de privacidad duplicado, `::1`, `testserver` fuera de los hosts de producción, mensajes de JSON inválido y cabeceras en errores.

Cambios de comportamiento de la CLI documentados:
- Un catálogo dañado ya no detiene `buscar` ni `consultar`: se informa como aviso en el estado del corpus.
- Algunos mensajes de error cambiaron de redacción; los códigos de salida coinciden.

## Límites

- Sin autenticación ni TLS: es una aplicación local. Exponerla exige un proxy con autenticación y TLS, decidido conscientemente.
- Las solicitudes de generación pueden tardar decenas de segundos y se atienden de una en una; no hay cola ni tareas en segundo plano.
- `/ready` calcula el SHA-256 de los PDF del catálogo y consulta Ollama (hasta 3 s) en cada llamada: sirve para comprobaciones manuales o de Docker, no para sondeos muy frecuentes.
