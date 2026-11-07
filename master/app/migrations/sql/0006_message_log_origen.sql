-- RF05 (A7): ciudad emisora y user_id AMQP de cada mensaje no aplicado.
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS city_id VARCHAR(16);
ALTER TABLE message_log ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_message_log_city_id ON message_log (city_id);
