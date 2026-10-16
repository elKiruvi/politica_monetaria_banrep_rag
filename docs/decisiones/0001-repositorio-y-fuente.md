# ADR 0001 — Aislamiento y fuente oficial

**Decisión:** conservar intacto el repositorio anterior y desarrollar el nuevo en un Git hermano. Descubrir informes desde el histórico oficial solo cuando la respuesta sea concluyente; nunca usar complementos para completar una ventana de doce.

**Evidencia:** el directorio nominal coincide con el original y contiene PDF protegidos; la página de Banrep mezcla tipos y las solicitudes paginadas recibieron bloqueo Radware.

**Opciones:** anidar un segundo Git, trasladar el original con copia o usar un hermano. El hermano evita modificar el original y no crea un subrepositorio accidental.

**Ventajas y riesgos:** aislamiento y procedencia claras; la ruta local nueva difiere del nombre de la raíz solicitada. El bloqueo impide validar el histórico completo por ahora. **Alternativa:** una interfaz oficial permitida, documentada y probada; no sortear restricciones.
