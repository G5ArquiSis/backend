# Plan de milestones

> RDOC02. Actualizado el 2026-11-07, después del merge de los PRs del mercado (C y D). Las fechas de lo cumplido son las de su merge a `main` o de
> su puesta en producción; las de lo pendiente y en revisión son la fecha objetivo. Las tareas de
> la E2 siguen la numeración del reparto por roles (A0 a E7). Se actualiza en cada hito.

Estados: **Cumplido** (en `main` o en producción), **Parcial** (una parte en `main`; se dice qué
falta), **En revisión** (PR abierto), **Pendiente**.

## Entrega E2

### M4 — Desbloqueo (Hora 0)

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| E0: migraciones SQL numeradas, flags `MARKET_ENABLED`/`WALLET_ENABLED`, rutas nuevas protegidas en Nginx | Infra | 2026-11-06 | Cumplido (PR #23; frontend PR #8) |
| A0: contrato I1, outbox con destino (`CENTRAL`, `MARKET_OFFER`, `CITY(code)`) | RF03 | 2026-11-06 | Cumplido (PR #24) |
| B0: contrato I2, interfaz de `WalletClient` y registro de handlers | RF03 | 2026-11-05 | Cumplido (PR #21) |
| C0: contrato I3, modelo de estrategia | AD6 | 2026-11-07 | Cumplido (PR #31) |
| D0: contrato I4, lectura del libro de ofertas | RF02 | 2026-11-07 | Cumplido (PR #37) |
| B7: ADR de AD5 (consistencia wallet-ledger) | RDOC01 | 2026-11-06 | Cumplido (PR #26). Se mergeó después que el código del PR #21 |
| C1: ADRs de AD4 (atención de bids) y AD6 (estrategia) | RDOC01 | 2026-11-07 | Cumplido (PR #32 y #33), antes que el código de C |

### M5 — Construcción

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| A1: consumidor de `market.TAL` | G02 | — | En revisión (PR #28) |
| A2: reglas de recepción de otras ciudades (`IDENTITY_MISMATCH`) y tipos retirados | G02, RF03 | — | En revisión (PR #28) |
| A3: publicación al exchange `market`, a `city.{CODE}` y a la central, con límite de tasa | RF03 | — | En revisión (PR #28) |
| A4: migración de la tabla outbox | RF03 | — | En revisión (PR #28) |
| A5: schemas JSON de los mensajes E2 | RDOC04 | 2026-11-07 | Cumplido: schemas en `contratos` (PR #5) y validación en el backend (PR #25) |
| A6: simulador de mercado | Pruebas | — | En revisión (PR #25) |
| A7: RF05 con los mensajes de la E2 | RF05 | 2026-11-07 | Cumplido: endpoint /message-log filtrable con remitente/fechas y contadores; vista Messages.jsx con visor crudo |
| B1: `WalletClient` real, idempotente y tolerante a 202, 429, 502 y 504 | RF03 | 2026-11-06 | Cumplido (PR #21, #27) |
| B2: eventos de wallet aplicados una sola vez y en cualquier orden | RF03, RF05 | 2026-11-06 | Cumplido (PR #21, #27) |
| B3: reconciliación con `GET /balance` (AD5) | AD5 | 2026-11-06 | Cumplido (PR #22). La dispara el heartbeat de connector |
| B4: ciclo autónomo con los mensajes extendidos de la central | G03 | 2026-11-06 | Cumplido (PR #22): el status-statement v3 fija el plazo del reporte con `market.reportOpensAt` e `imbalance-settlement` se aplica. Se verifica en producción al encender los flags (M6) |
| B5: RF01, historial de ciclos completo | RF01 | — | Pendiente |
| B6: tests de idempotencia, desorden y reconciliación | RF05 | 2026-11-06 | Parcial: tests de la wallet en `main` (PR #27) |
| C2 a C5: oferta propia, bids en serie (AD4), swap del vendedor y estrategia | G04, RF04 | 2026-11-07 | Cumplido (PR #34, #35, #36 y #38; contratos PR #9) |
| C6: RF04, pantalla de administración del mercado | RF04 | — | En revisión (frontend PR #11) |
| D1 a D6: libro de ofertas, RF02, motor comprador, verificación del token, RF06 | G04, RF02, RF06 | 2026-11-07 | Cumplido (PR #37, contratos PR #8, frontend PR #10). Detalle en [comprador.md](comprador.md). El comprador entra apagado (`BUYER_ENABLED=false`) |
| E1: wallet y broker simulados (`scripts/fake_wallet/`) | Pruebas | 2026-11-06 | Cumplido (PR #29) |
| E2: métricas de mercado y wallet en New Relic, dashboard y alertas | G08, RNF05 | 2026-11-06 | Cumplido (PR #29). Dashboard importado y seis alertas creadas en la política "EnergyShark E2", con aviso por email; la del feed del mercado queda desactivada hasta encender `MARKET_ENABLED` |
| E3: rollback del deploy y verificación de requisitos esenciales | G01, RNF04 | 2026-11-06 | Cumplido (PR #29) |
| E4: contract tests en CI y OpenAPI al día | RDOC04 | 2026-11-06 | Cumplido (PR #29, contratos PR #6). fastapi y pydantic quedan fijados para que el OpenAPI generado no cambie con la versión |

### M6 — Mercado en producción

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Hito H3: primer swap liquidado en producción | G04 | — | Pendiente: C4 y D4 ya están en `main`; falta encender los flags |
| Encender `MARKET_ENABLED`, `WALLET_ENABLED` y `BUYER_ENABLED` en la EC2 | G02, G03 | — | Pendiente |
| Gates G01 a G09 con evidencia ([gates-e2.md](gates-e2.md)) | Gates | Antes de la demo | Parcial: G01, G05, G06, G07, G08 y G09 comprobados (2026-11-06) |
| D7: correr el experimento de span y escribir el informe | RF06, RDOC05 | — | Pendiente: método y herramientas listos ([experimento-span.md](experimento-span.md)); necesita el comprador encendido y la pantalla de estrategia (C6, frontend PR #11) |

### M7 — Calidad, documentación y demo

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| E5: spec, milestones, tabla de AI logs, UML y docs del flujo v3 | RDOC02, RDOC03 | 2026-11-06 | Cumplido (PR #29); se actualiza con los PRs del mercado |
| E6: escenario de punta a punta, ensayo de caída y guardián de ciclos | G03 | 2026-11-06 | Cumplido (PR #29). El escenario pasa hasta el reporte; la parte del mercado espera a C y D |
| E7: scripts de las anomalías 1 a 7 ([scripts/ensayos/README.md](../scripts/ensayos/README.md)) | Demo | 2026-11-06 | Cumplido (PR #29). 5, 6 y 7 pasan; el resto espera a A2, C y D |
| Post-mortems de AD1 (A8), AD2 (B7) y AD3 (D8) | RDOC01 | — | Parcial: Post-mortem de AD1 (A8) completado en ADR-0001; B7 y D8 pendientes de cierre |

## Entrega E1

### M0 — Base del proyecto

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
| OpenAPI completo, generado desde la app, y schemas de los 12 mensajes | RDOC04 | 2026-11-04 | Cumplido (contratos PR #4) |

### M1 — Infraestructura

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

### M2 — Funcionalidad de la E1

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Consumir `city.{cityId}`, validar el envelope y responder ACK/NACK | G02 | 2026-10-07 | Cumplido (PR #7, #11, #13) |
| Ledger: aplicar `status-statement`, `transfer` y `demand-statement` | RF03 | 2026-10-07 | Cumplido (PR #8, #12) |
| Registro de duplicados y de mensajes descartados o con NACK | RF05 | 2026-10-07 | Cumplido |
| No responder NACK a un `ack`/`nack`/`error` inválido; se descarta y se registra | RF03, RF05 | 2026-11-03 | Cumplido (PR #19) |
| `negotiation-report` dentro de la ventana, en todos los ciclos | G03, RF03 | 2026-10-07 | Cumplido |
| Pedir el `status-statement` (y la `distance-table`) con `request` si no llega | RF03 | 2026-11-03 | Cumplido (PR #19) |
| Negociación voluntaria con timeouts de 30 s y reintento con el mismo `idpk` | RF03 | 2026-10-07 | Cumplido. Retirada en la E2 (`POST /negotiations` responde 410) |
| Tenant de Auth0 y login en la SPA | G06 | 2026-10-08 | Cumplido |
| Vista de historial de ciclos | RF01 | 2026-10-08 | Cumplido en versión básica |
| Detalle de ciclo completo: status-statement, fondos, demand-statements, negociaciones, reporte y última operación | RF01 | 2026-11-04 | Cumplido (frontend PR #7) |
| Vista de conectividad (`distance-table`), actualizada cada 30 s | RF02 | 2026-11-04 | Cumplido (frontend PR #7) |
| Interfaz de administración de negociaciones | RF04 | 2026-10-08 | Cumplido |

### M3 — Cierre

| Tarea | Requisito | Fecha | Estado |
|---|---|---|---|
| Varios ciclos reales corriendo en la nube sin intervención | G03 | 2026-11-03 | Cumplido: 25 ciclos seguidos con reporte enviado |
| Diagrama UML de componentes y guía para correr en local | RDOC03 | 2026-11-03 | Cumplido (PR #20); actualizado para la E2 en M7 |
| Pasos para replicar el flujo de monitoreo | RDOC03 | 2026-10-07 | Cumplido (PR #5) |
| Ensayo de las anomalías de la demo | Demo | 2026-11-06 | Reemplazado por E7 (M7) |
| Post-mortem en los ADRs | RDOC01 | Después de la demo | Pendiente (M7) |
