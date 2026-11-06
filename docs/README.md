# Documentación

> El curso no entrega un template: la estructura y los formatos de esta carpeta son los del grupo.
> Las letras de la columna "Responsable" son los roles de la E2 ([spec.md](spec.md#roles)).

| Documento | Requisito | Responsable |
|---|---|---|
| [spec.md](spec.md) — objetivo, alcance, roles y forma de trabajo de la E2 (y la spec de la E1) | RDOC02 | E |
| [milestones.md](milestones.md) — tareas A0 a E7 con su estado real | RDOC02 | E, con todos |
| [ai-logs/](ai-logs/) — tabla y un archivo por integrante | RDOC02 | Todos |
| [adr/](adr/) — AD1, AD2, AD3 y AD5; AD4 y AD6 pendientes; post-mortems después de la demo | RDOC01 | A (AD1), B (AD2, AD5), C (AD4, AD6), D (AD3) |
| [arquitectura.md](arquitectura.md) — diagrama UML de componentes, cómo correr en local y tests | RDOC03 | E |
| [ciclo-negociacion.md](ciclo-negociacion.md) — ledger, eventos de la wallet, reconciliación, reporte, API y contrato con `connector` | RF01 a RF05 | B, con A |
| [monitoreo.md](monitoreo.md) — New Relic: instalación, métricas de la E2, dashboard, alertas y guardián de ciclos | RDOC03, RNF05 | E |
| [infraestructura.md](infraestructura.md) — runbook de AWS y deploy | RNF04 | E |
| [gates-e2.md](gates-e2.md) — checklist de los gates G01 a G09 con su evidencia | Gates E2 | E |
| [`scripts/ensayos/README.md`](../scripts/ensayos/README.md) — ensayos de las anomalías de la demo, escenario de punta a punta y caída de contenedores | Demo | E |
| [`scripts/fake_wallet/README.md`](../scripts/fake_wallet/README.md) — wallet y broker falsos para desarrollo local | Pruebas | E |

Los schemas de mensajes y el OpenAPI (RDOC04) viven en el repo
[`contratos`](https://github.com/G5ArquiSis/contratos). El CI comprueba que el OpenAPI coincida
con la app y valida los mensajes que publica el nodo contra los schemas del curso y del grupo.
