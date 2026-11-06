-- Desempate determinista de eventos con el mismo timestamp; /balance tiene prioridad.
ALTER TABLE wallet_balance ADD COLUMN IF NOT EXISTS event_key VARCHAR(64) NOT NULL DEFAULT '';
