#!/bin/sh
# Arranca el servicio bajo el agente de New Relic (APM, RNF05) solo si hay license key.
# Sin NEW_RELIC_LICENSE_KEY (desarrollo local, CI) corre igual, sin agente.
set -e

if [ -n "${NEW_RELIC_LICENSE_KEY:-}" ]; then
    exec newrelic-admin run-program "$@"
fi

exec "$@"
