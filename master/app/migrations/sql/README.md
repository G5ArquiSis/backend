# Migraciones SQL

master las aplica al arrancar, después de `create_all` (ver `../__init__.py`). Cada archivo
corre una sola vez, y su versión queda registrada en la tabla `schema_migrations`.

## Cuándo hace falta una

`create_all` crea las tablas nuevas, pero no altera las existentes. Si tu cambio en
`models.py` **agrega, cambia o borra una columna o un índice de una tabla que ya existe en
producción**, necesitas una migración. Si solo agregas una tabla nueva, no.

## Cómo escribirla

1. Crea un archivo `NNNN_descripcion.sql` con el siguiente número libre (`0001`, `0002`...).
2. Hazla **idempotente**. En una base nueva, `create_all` ya creó la tabla con su forma final
   antes de que corra la migración:

   ```sql
   ALTER TABLE outbox ADD COLUMN IF NOT EXISTS exchange VARCHAR(64);
   ALTER TABLE outbox ADD COLUMN IF NOT EXISTS routing_key VARCHAR(128);
   CREATE INDEX IF NOT EXISTS ix_outbox_exchange ON outbox (exchange);
   ```
3. Si la columna nueva es `NOT NULL`, dale un `DEFAULT` o rellena las filas existentes en la
   misma migración.
4. Actualiza también `models.py`, para que una base nueva quede igual que una migrada.
5. Pruébala contra una base que tenga el esquema anterior. Por ejemplo, levanta `main` con
   `docker compose up`, cambia a tu rama y vuelve a levantar.

**Nunca edites una migración ya mergeada**: producción ya la aplicó y no la volverá a correr.
Para corregirla, agrega otra.

Todas las migraciones pendientes corren en una sola transacción. Si una falla, master no
arranca: revisa el log del contenedor (`docker compose logs master-1`).
