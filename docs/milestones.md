# Plan de milestones

> RDOC02. Actualizado el 2026-11-03. Las fechas de lo cumplido son las de sus commits o de su
> puesta en producción; las de lo pendiente y en revisión son la fecha objetivo.

Estados: **Cumplido** (en `main` o en producción), **En revisión** (PR abierto), **Pendiente**.

## M0 — Base del proyecto

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Organización de GitHub y repos `backend`, `frontend` y `contratos` | G04 | 2026-09-27 | Cumplido |
| Código de la E0 (`master`, `connector`, tests) portado al backend | E0 | 2026-09-28 | Cumplido |
| `main` protegido: PR con 2 aprobaciones | Uso de IA | 2026-09-28 | Cumplido |
| Contexto compartido `contratos/AGENTS.md`, enlazado desde ambos README | RDOC04 | 2026-10-06 | Cumplido |
| Formato de AI logs del grupo | RDOC02 | 2026-10-06 | Cumplido |
| Spec de la entrega y plan de milestones | RDOC02 | 2026-10-07 | Cumplido |
| Reparto de roles A a D | — | 2026-10-07 | Cumplido |
| ADRs de AD1, AD2 y AD3 | RDOC01 | 2026-10-08 | Cumplido |
| OpenAPI de `/cycles` y `/message-log` | RDOC04 | 2026-10-08 | Cumplido (contratos PR #3) |
| OpenAPI completo, generado desde la app, y schemas de los 12 mensajes | RDOC04 | 2026-11-03 | En revisión (rama `docs/contrato-api-y-mensajes` en `contratos`) |

## M1 — Infraestructura

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Budget alerts en la cuenta de AWS | G08 | 2026-09-28 | Cumplido |
| Deploy automático del backend: ECR + EC2 con compose de producción | G01, RNF04 | 2026-09-28 | Cumplido |
| Frontend en S3 + CloudFront, con deploy automático | RNF03 | 2026-09-28 | Cumplido |
| HTTPS en backend y frontend | G05 | 2026-09-28 | Cumplido |
| Balanceo de carga sobre dos réplicas de `master` | E0 | 2026-10-06 | Cumplido |
| API Gateway con subdominio `api.melchort.me` y CORS | RNF01 | 2026-10-07 | Cumplido (PR #4) |
| Dominio propio del frontend, `app.melchort.me` | — | 2026-10-06 | Cumplido |
| Monitoreo de infraestructura con New Relic | G07, RNF05 | 2026-10-06 | Cumplido |
| APM de New Relic en `master` | RNF05 | 2026-10-07 | Cumplido (PR #5) |
| Autorizador JWT en API Gateway | RNF02 | 2026-10-08 | Cumplido; sin token o con uno inválido responde 401 |

## M2 — Funcionalidad de la E1

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Consumir `city.{cityId}`, validar el envelope y responder ACK/NACK | G02 | 2026-10-07 | Cumplido (PR #7, #11, #13) |
| Ledger: aplicar `status-statement`, `transfer` y `demand-statement` | RF03 | 2026-10-07 | Cumplido (PR #8, #12) |
| Registro de duplicados y de mensajes descartados o con NACK | RF05 | 2026-10-07 | Cumplido |
| No responder NACK a un `ack`/`nack`/`error` inválido; se descarta y se registra | RF03, RF05 | 2026-11-03 | En revisión (rama `feat/rf03-request-y-no-nack`) |
| `negotiation-report` dentro de la ventana, en todos los ciclos | G03, RF03 | 2026-10-07 | Cumplido |
| Pedir el `status-statement` (y la `distance-table`) con `request` si no llega | RF03 | 2026-11-03 | En revisión (rama `feat/rf03-request-y-no-nack`) |
| Negociación voluntaria con timeouts de 30 s y reintento con el mismo `idpk` | RF03 | 2026-10-07 | Cumplido |
| Tenant de Auth0 y login en la SPA | G06 | 2026-10-08 | Cumplido |
| Vista de historial de ciclos | RF01 | 2026-10-08 | Cumplido en versión básica |
| Detalle de ciclo completo: status-statement, fondos, demand-statements, negociaciones, reporte y última operación | RF01 | 2026-11-03 | En revisión (rama `feat/rf01-rf02-detalle-y-conectividad` en `frontend`) |
| Vista de conectividad (`distance-table`), actualizada cada 30 s | RF02 | 2026-11-03 | En revisión (misma rama del frontend) |
| Interfaz de administración de negociaciones | RF04 | 2026-10-08 | Cumplido |

## M3 — Cierre

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Varios ciclos reales corriendo en la nube sin intervención | G03 | 2026-11-03 | Cumplido: 25 ciclos seguidos con reporte enviado |
| Diagrama UML de componentes y guía para correr en local | RDOC03 | 2026-11-03 | En revisión ([arquitectura.md](arquitectura.md)) |
| Pasos para replicar el flujo de monitoreo | RDOC03 | 2026-10-07 | Cumplido (PR #5) |
| Ensayo de las anomalías de la demo (`scripts/demo_anomalia_1.py`) | Demo | 2026-11-03 | En revisión |
| Post-mortem en los ADRs | RDOC01 | Después de la demo | Pendiente |
