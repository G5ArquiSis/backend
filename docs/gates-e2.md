# Gates de la E2: checklist con evidencia

Sin cualquiera de estos gates la nota máxima es 4,0 (enunciado E2 § 4). Cada fila dice cómo se
comprueba y dónde queda la evidencia. Se revisa antes de la entrega y antes de la demo; la
persona de infraestructura (rol E) mantiene este archivo.

Estado al 2026-11-05: ✅ comprobado · ⏳ depende de trabajo en curso · ❌ falta.

| Gate | Qué exige | Cómo se comprueba | Estado | Evidencia |
|---|---|---|---|---|
| G01 | La app corre completa en la nube (EC2 free tier) | `curl https://api.melchort.me/health` → 200. En la EC2, `docker compose ... ps`: los cuatro contenedores (postgres, master-1, master-2, connector) `healthy` | ✅ para la E1 | `/health` 200 el 2026-11-05. Volver a comprobar con el mercado encendido |
| G02 | Consume sus dos colas y responde ACK/NACK según las reglas, también a otras ciudades | Logs de connector: `Escuchando la cola city.TAL.q` y `market.TAL`. `GET /message-log` muestra ACK y NACK, incluidos mensajes de otras ciudades. El healthcheck de connector exige que latan `ciudad` y `mercado` | ⏳ | El consumidor de `market.TAL` y los tipos de la E2 en connector aún no existen (hoy los eventos `swap-*` reciben `NACK UNKNOWN_TYPE`) |
| G03 | Ciclo autónomo: status-statement, eventos de la wallet, `imbalance-settlement` y `negotiation-report` en **todos** los ciclos | `GET /cycles`: todos los ciclos cerrados tienen reporte enviado. Alerta de New Relic "ciclo sin reporte" sin disparos (`docs/monitoreo.md`) | ⏳ | El reporte de la E1 sale. Faltan los eventos de la wallet y el `imbalance-settlement` |
| G04 | Al menos **un swap liquidado** en el mercado de producción, dentro del periodo de puntaje | `GET /v1/statement` de la wallet real con un asiento `kind: swap`. En nuestro ledger, el `swap-settled` y su `trade-recorded` | ❌ | Anotar `swapId`, `cycleId`, fecha y captura del historial de ciclos (RF01) |
| G05 | Backend (JSON) y frontend (SPA) en repos distintos | `G5ArquiSis/backend` y `G5ArquiSis/frontend` | ✅ | Los dos repos de la organización |
| G06 | HTTPS en ambos | `curl -I https://api.melchort.me/health` y `curl -I https://app.melchort.me`, ambos 200 con certificado válido | ✅ | Comprobado el 2026-11-05 (`ssl_verify_result` 0 en ambos) |
| G07 | Auth0 o Cognito con JWK | `curl https://api.melchort.me/cycles` sin token → 401. Con el token de Auth0 → 200. Autorizador JWT del gateway con el issuer de Auth0 (`deploy/api-gateway/autorizador.sh`) | ✅ | 401 sin token el 2026-11-05 |
| G08 | Monitoreo SaaS (New Relic) | APM de master y agente de infraestructura en la EC2. **E2:** también el consumidor del mercado y las llamadas a la wallet (RNF05) | ⏳ | APM e infraestructura vienen de la E1. Las métricas del mercado y la wallet son la tarea E2 del rol E (`docs/monitoreo.md`) |
| G09 | Budget alerts en AWS | `aws budgets describe-budgets --account-id <cuenta>` | ✅ | Presupuesto `energyshark-mensual`, USD 5 mensuales |

## Antes de la demo

1. Correr de nuevo todas las comprobaciones de la tabla y actualizar estado y fecha.
2. G04: confirmar que el swap liquidado cae **dentro del periodo de puntaje**: su ciclo tiene
   `opensAt` igual o posterior a la apertura del mercado de producción.
3. Con `MARKET_ENABLED=true` y `WALLET_ENABLED=true` en la EC2, `docker compose ps` debe mostrar
   connector `healthy`: eso confirma que también late el consumidor del mercado.
4. Guardar las capturas (New Relic, historial de ciclos con el swap y el budget) en la carpeta de
   la entrega.
