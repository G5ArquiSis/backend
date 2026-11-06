-- ROL B: metadata v3, incluida disabledRouteTransportCost; no cambia rutas existentes.
ALTER TABLE distance_tables ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}';
