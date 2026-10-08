# AI log — Vicentepvz

- **Integrante:** Vicente Pavez
- **Autocompletado:** no

## 2026-10-07 — Análisis y redacción de ADR-0002 (AD2: Persistencia del ledger)

- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Evaluar la estrategia de persistencia del ledger (Snapshot vs. Event Sourcing vs. Híbrido, SQL vs. NoSQL) y redactar el ADR-0002 para cumplir con AD2.
- **Prompts relevantes:**
  > "Ayudame a reescribir y validar este ADR, quiero que quede bien hecho y que cumpla con todos los requerimientos del enunciado"

- **Qué produjo la IA:** En el documento se formalizó el modelo híbrido (Event Log inmutable en `ledger_events` + proyección de estado consolidado en `cycle_ledger` sobre PostgreSQL), el esquema relacional con sus tipos de datos, la convención de signos de `demand-statement`, el manejo de traspaso de presupuestos/energía, y la garantía de idempotencia a nivel de base de datos (`UNIQUE(idpk)`) para RF05 y la Anomalía 1 de la demo.
- **Verificación:** Validación contra los requerimientos del enunciado E1 (reconstruibilidad de AD2, lecturas O(1) para RF01, idempotencia para RF05) y la plantilla `docs/adr/0000-plantilla.md`.
- **Correcciones del integrante:** Se priorizó pulir en detalle el ADR antes de generar el log, se ajustó la convención de nomenclatura del archivo para evitar colisiones con AD1 (`0002-persistencia-ledger.md`).
- **Referencia:** Rama `feature/ad2-persistencia-ledger`, commits de `docs/adr/` y PR de AD2.
