# ADR-0006: Estrategia de mercado como configuración versionada



- **Estado:** propuesto

- **Fecha:** 2026-11-06

- **Responsable:** Rol C

- **Área del enunciado:** AD6



## Contexto



El comportamiento del mercado depende de distintos parámetros de estrategia: precio y cantidad de la oferta, política de aceptación de bids, tiempo de espera de compra (`span`) y política de selección de ofertas.



Estos parámetros son utilizados tanto por el lado vendedor como por el comprador. El contrato interno `StrategyConfig` y la función `get_active_strategy(cycle_id)` permiten que ambos lados consulten una configuración común: C utiliza el precio, la cantidad y la política de aceptación; D utiliza el `span` y la política de selección.



El requisito AD6 considera que durante la ejecución del sistema se pueden modificar parámetros de estrategia, incluso a mitad de un ciclo. Los cambios deben aplicarse en el siguiente ciclo, sin redeploy ni reinicio, y debe quedar registrado qué estrategia se utilizó en cada ciclo.



Esta trazabilidad también es necesaria para el experimento RF06, que debe relacionar los resultados de cada ciclo con el `span` efectivamente utilizado.



## Alternativas consideradas



1. **Configuración definida en código o variables de entorno.**

  - Ventajas: implementación sencilla y fácil de mantener.

  - Desventajas: los cambios requieren modificar el código o el entorno y desplegar o reiniciar el servicio. No satisface el requisito de administración dinámica.



2. **Configuración mutable única, consultada en cada operación.**

  - Ventajas: permite modificar parámetros mediante una API sin redeploy.

  - Desventajas: una modificación a mitad de ciclo podría provocar que distintas operaciones utilicen configuraciones diferentes. Además, dificulta reconstruir qué estrategia se ejecutó durante un ciclo.



3. **Configuraciones versionadas con activación por ciclo y snapshot persistido.**

  - Ventajas: permite modificar parámetros sin reiniciar el sistema, mantener una estrategia consistente durante cada ciclo y registrar qué versión se utilizó.

  - Desventajas: requiere persistir versiones de configuración, administrar su activación y guardar un snapshot asociado a cada ciclo.



## Decisión



Se propone utilizar configuraciones de estrategia versionadas y persistidas en PostgreSQL, separando las políticas de mercado del mecanismo que las ejecuta.



Cada configuración contendrá los parámetros definidos por el contrato `StrategyConfig`: precio o fórmula de la oferta, cantidad, política de aceptación de bids, `span` de espera de compra y política de selección de ofertas.



Las políticas se representarán mediante identificadores y parámetros validados. Su lógica de ejecución permanecerá implementada en el backend, evitando almacenar o ejecutar código arbitrario como configuración.



El funcionamiento será el siguiente:



1. Los cambios realizados desde la API de administración se guardarán como una nueva versión de la estrategia.

2. La configuración modificada no alterará los ciclos que ya se encuentran en ejecución.

3. Al iniciar un nuevo ciclo, se seleccionará la última configuración disponible para ese ciclo.

4. La configuración seleccionada se persistirá como un snapshot asociado al `cycle_id`.

5. Durante ese ciclo, las operaciones de mercado utilizarán el snapshot correspondiente, sin volver a consultar una configuración que pueda cambiar a mitad de la ejecución.

6. La función `get_active_strategy(cycle_id)` permitirá obtener la configuración correspondiente al ciclo. C y D utilizarán la misma versión.

7. Los cambios efectuados durante un ciclo quedarán disponibles para el siguiente, sin requerir redeploy ni reinicio del backend.



El snapshot conservará los parámetros efectivamente utilizados, especialmente el `span`, para permitir reconstruir la estrategia aplicada en cada ciclo y relacionarla con los resultados del experimento RF06.



## Consecuencias y tradeoffs



- Los parámetros de estrategia podrán modificarse desde la API de administración sin desplegar nuevamente el backend.

- Los cambios tendrán efecto a partir del siguiente ciclo, evitando alterar operaciones en curso.

- C y D utilizarán una configuración común y consistente para cada ciclo.

- Los snapshots permitirán identificar qué estrategia se ejecutó en cada ciclo.

- RF06 podrá atribuir los resultados del experimento al `span` efectivamente utilizado.

- Se agrega complejidad por la persistencia, versionamiento y administración de configuraciones.

- La incorporación de una política algorítmica completamente nueva seguirá requiriendo implementar su mecanismo en el backend; la configuración permitirá seleccionar entre las políticas que ya estén implementadas.



## Post-mortem (después de la demo)



Pendiente de completar después de la demo, documentando los cambios de estrategia realizados, su aplicación entre ciclos y la trazabilidad obtenida en el experimento RF06.
