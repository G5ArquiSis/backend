# AI log - CatalinaBO

* **Integrante:** Catalina Berríos
* **Proyecto**: EnergyShark - Entrega 1
**Autocompletado:** no

## Registro de Sesiones

## 2026-10-07 — Corrección de errores y compatibilidad en CI/CD

* **Herramienta**: Asistente de IA (Gemini), modo chat


* **Tarea**: Consultar la causa de fallos específicos reportados en el Pull Request (PR #7) y ajustar detalles puntuales de compatibilidad para el despliegue en producción.

* **Prompts Relevantes:**:

  * "¿Por qué falla la importación de `pika` en el contenedor y cómo resolver el conflicto de dependencias con `aio-pika`?"
  * "Explicar el error `TypeError` en el constructor de `MasterClient` al pasarle parámetros y cómo solucionarlo manteniendo la inicialización previa."
  * "¿Cómo corregir las advertencias del linter (Ruff) por variables no importadas como `Optional` y nombres de funciones mal referenciados?"

* **Qué produjo la IA:**:

  * Detección de la dependencia faltante en `requirements.txt`.
  * Ajuste en el constructor de `MasterClient` para evitar discrepancias de argumentos y permitir llamadas síncronas acordes a la librería `pika`.
  * Corrección de nombres de funciones (`procesar_mensaje_y_responder`) y resolución de advertencias de sintaxis detectadas por el CI.

**Verificación:**

Ejecución local de tests unitarios (`pytest connector/tests/`), validación con linter (`ruff check .`) y paso exitoso de la suite en GitHub Actions.

* **Correcciones del integrante**:

  * Se revisó y adaptó manualmente cada sugerencia para conservar la estructura del proyecto y la nomenclatura en español elegida para el código.
  * El diseño de la arquitectura, la lógica del consumidor y los modelos de base de datos fueron definidos e implementados directamente según las especificaciones del enunciado.

* **Referencia:** PR #7


## 2026-11-03 — Revisión y pruebas del rol B (Wallet)

* **Herramienta**: Asistente de IA (ChatGPT), modo codex y chat.

* **Tarea**: Revisar los avances que ya se tenía implementados para el Rol B de la Entrega 2, identificar los errores puntuales que estos tenían  y ejecutar pruebas para verificar el funcionamiento de las nuevas funcionalidades del protocolo v3.

* **Prompts Relevantes:**:

  * "Revisa los avances que ya tengo implementados para el Rol B e identifica posibles errores o incompatibilidades con el protocolo v3"
  * "Implementa los tests correspondientes a las nuevas funcionalidades y ejecútalos para comprobar su funcionamiento"
  * "Explicame los errores circulares que estoy teniendo"

* **Qué produjo la IA:**:

  * Revisión y ajustes faltantes en la integración con la Wallet.
  * Correcciones relacionadas con la consistencia de los datos en el ledger.
  * Incorpora las pruebas para las nuevas funcionalidades del protocolo v3 .

**Verificación:**

Ejecución local de tests unitarios validación con linter (`ruff check .`) y paso exitoso de la suite en GitHub Actions.

* **Correcciones del integrante**:

  * Se revisó y adaptó manualmente cada sugerencia para conservar la estructura del proyecto y cumplir con los nuevos requisitos de esta entrega.
  * El diseño de la arquitectura, la lógica de la wallet y los modelos de base de datos fueron definidos e implementados directamente según las especificaciones del enunciado.

  