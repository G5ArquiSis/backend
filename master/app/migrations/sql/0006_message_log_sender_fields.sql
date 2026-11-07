-- Migración 0006: Agrega sender_city y sender_user_id a message_log (Tarea A7 / RF05)
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS sender_city VARCHAR(64);
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS sender_user_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_message_log_sender_city ON message_log (sender_city);
CREATE INDEX IF NOT EXISTS ix_message_log_category ON message_log (category);
