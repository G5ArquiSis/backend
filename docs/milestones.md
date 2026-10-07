# Plan de milestones

> RDOC02. Actualizado el 2026-10-07. Las fechas de lo cumplido son las de sus commits o de su
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
| Spec de la entrega y plan de milestones | RDOC02 | 2026-10-07 | En revisión |
| Reparto de roles A a D | — | 2026-10-07 | Cumplido |
| ADRs de AD1, AD2 y AD3 | RDOC01 | 2026-10-07 | Pendiente |
| Schemas de mensajes y OpenAPI de los endpoints de la E1 | RDOC04 | 2026-10-07 | Pendiente |

## M1 — Infraestructura

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Budget alerts en la cuenta de AWS | G08 | 2026-09-28 | Cumplido |
| Deploy automático del backend: ECR + EC2 con compose de producción | G01, RNF04 | 2026-09-28 | Cumplido |
| Frontend en S3 + CloudFront, con deploy automático | RNF03 | 2026-09-28 | Cumplido |
| HTTPS en backend y frontend | G05 | 2026-09-28 | Cumplido |
| Balanceo de carga sobre dos réplicas de `master` | E0 | 2026-10-06 | Cumplido |
| API Gateway con subdominio `api.melchort.me` y CORS | RNF01 | 2026-10-06 | Cumplido en producción; documentación en revisión (PR #4) |
| Dominio propio del frontend, `app.melchort.me` | — | 2026-10-06 | Cumplido en producción; documentación en revisión |
| Monitoreo de infraestructura con New Relic | G07, RNF05 | 2026-10-06 | Cumplido |
| APM de New Relic en `master` | RNF05 | 2026-10-07 | En revisión (PR #5) |
| Autorizador JWT en API Gateway | RNF02 | 2026-10-07 | Pendiente; depende de Auth0 |

## M2 — Funcionalidad de la E1

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Consumir `city.{cityId}`, validar el envelope y responder ACK/NACK | G02 | 2026-10-07 | Pendiente |
| Ledger: aplicar `status-statement`, `transfer` y `demand-statement` | RF03 | 2026-10-07 | Pendiente |
| Registro de duplicados y de mensajes descartados o con NACK | RF05 | 2026-10-07 | Pendiente |
| `negotiation-report` dentro de la ventana, en todos los ciclos | G03, RF03 | 2026-10-07 | Pendiente |
| Negociación voluntaria con timeouts de 30 s y reintento con el mismo `idpk` | RF03 | 2026-10-07 | Pendiente |
| Tenant de Auth0 y login en la SPA | G06 | 2026-10-07 | Pendiente |
| Vista de historial de ciclos | RF01 | 2026-10-07 | Pendiente |
| Vista de conectividad (`distance-table`) | RF02 | 2026-10-07 | Pendiente |
| Interfaz de administración de negociaciones | RF04 | 2026-10-07 | Pendiente |

## M3 — Cierre

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Varios ciclos reales corriendo en la nube sin intervención | G03 | 2026-10-07 | Pendiente |
| Diagrama UML de componentes y guía para correr en local | RDOC03 | 2026-10-07 | Pendiente |
| Pasos para replicar el flujo de monitoreo | RDOC03 | 2026-10-07 | En revisión (PR #5) |
| Ensayo de las anomalías de la demo | Demo | 2026-10-07 | Pendiente |
| Post-mortem en los ADRs | RDOC01 | 2026-10-07 | Pendiente |
