-- Marca de cuándo se informó a New Relic el cierre de cada ciclo (evento CycleClosed).
ALTER TABLE cycle_ledger ADD COLUMN IF NOT EXISTS closure_recorded_at TIMESTAMPTZ;

-- Los ciclos que ya cerraron antes de esta migración no se informan: serían cientos de
-- eventos de golpe, fechados hoy, que distorsionarían el dashboard.
UPDATE cycle_ledger
SET closure_recorded_at = now()
WHERE closure_recorded_at IS NULL AND valid_until <= now();
