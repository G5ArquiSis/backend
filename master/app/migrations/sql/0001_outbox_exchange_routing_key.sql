-- Migración 0001: Agrega exchange y routing_key a la tabla outbox (Tarea A4)
-- Idempotente: filas viejas de la E1 quedan con destino a la central por defecto.

ALTER TABLE outbox ADD COLUMN IF NOT EXISTS exchange VARCHAR(64) NOT NULL DEFAULT 'energy.x';
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS routing_key VARCHAR(128) NOT NULL DEFAULT 'central';

UPDATE outbox SET exchange = 'energy.x' WHERE exchange IS NULL;
UPDATE outbox SET routing_key = 'central' WHERE routing_key IS NULL;

CREATE INDEX IF NOT EXISTS ix_outbox_exchange ON outbox (exchange);
