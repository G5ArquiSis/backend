#!/usr/bin/env bash
# Deploy en la EC2. Lo copia y ejecuta el workflow de CI vía SSM, como root, en
# /opt/energyshark; no se corre a mano salvo para depurar.
#
#   remote-deploy.sh <MASTER_IMAGE> <CONNECTOR_IMAGE>
#
# Espera junto a él, escritos por el workflow, docker-compose.prod.yml.new y
# energyshark-nginx.conf.new (los de ese commit).
#
# Rollback automático: antes de tocar nada respalda la versión que está corriendo
# (release.env y docker-compose.prod.yml → *.prev). Si la nueva no queda healthy dentro
# del plazo, vuelve a levantar la anterior y sale con error, para que el job de CI quede
# rojo aunque producción siga funcionando. El rollback manual a un commit anterior es el
# workflow_dispatch con `sha` (docs/infraestructura.md § Rollback).
set -euo pipefail

MASTER_IMAGE=${1:?falta MASTER_IMAGE}
CONNECTOR_IMAGE=${2:?falta CONNECTOR_IMAGE}
# Sobrescribibles sólo para los tests (tests/test_remote_deploy.py).
APP_DIR=${APP_DIR:-/opt/energyshark}
SITE=${NGINX_SITE:-/etc/nginx/sites-available/energyshark}

cd "$APP_DIR"
test -f .env || { echo "falta $APP_DIR/.env"; exit 1; }
test -f docker-compose.prod.yml.new || { echo "falta docker-compose.prod.yml.new"; exit 1; }

compose() {
    docker compose --env-file .env --env-file release.env -f docker-compose.prod.yml "$@"
}

if [ -f release.env ] && [ -f docker-compose.prod.yml ]; then
    cp release.env release.env.prev
    cp docker-compose.prod.yml docker-compose.prod.yml.prev
    echo "Versión anterior respaldada: $(tr '\n' ' ' < release.env.prev)"
fi
mv docker-compose.prod.yml.new docker-compose.prod.yml
printf 'MASTER_IMAGE=%s\nCONNECTOR_IMAGE=%s\n' "$MASTER_IMAGE" "$CONNECTOR_IMAGE" > release.env

# Las réplicas de master se actualizan de a una y se espera a que cada una quede healthy
# antes de tocar la otra: Nginx siempre tiene una a la que enviar tráfico. El último `up`
# levanta connector (healthy = todos sus consumidores laten) y elimina huérfanos.
rollout() {
    compose pull &&
    compose up -d --no-deps --wait --wait-timeout 120 postgres &&
    compose up -d --no-deps --wait --wait-timeout 120 master-1 &&
    compose up -d --no-deps --wait --wait-timeout 120 master-2 &&
    compose up -d --remove-orphans --wait --wait-timeout 180
}

rollback() {
    echo "=== La versión nueva no quedó healthy; estado de los contenedores ==="
    compose ps -a || true
    # Los logs NO se imprimen: esta salida queda en GitHub Actions, y un error de
    # configuración de pydantic muestra los valores del .env (contraseñas incluidas).
    # Se leen en la EC2: docs/infraestructura.md § Rollback.
    if [ ! -f release.env.prev ]; then
        echo "No hay versión anterior respaldada: no se puede volver atrás."
        return 1
    fi
    echo "=== Rollback a: $(tr '\n' ' ' < release.env.prev) ==="
    cp release.env.prev release.env
    cp docker-compose.prod.yml.prev docker-compose.prod.yml
    compose up -d --remove-orphans --wait --wait-timeout 180
}

if ! rollout; then
    if rollback; then
        echo "ROLLBACK OK: producción sigue con la versión anterior. El deploy falló."
    else
        echo "ROLLBACK FALLIDO: revisar la EC2 a mano (docs/infraestructura.md § Rollback)."
    fi
    exit 1
fi
echo "Deploy OK: $(tr '\n' ' ' < release.env)"

# Nginx se actualiza sólo si cambió, y sólo si la config nueva pasa `nginx -t`.
if [ -f energyshark-nginx.conf.new ]; then
    if cmp -s energyshark-nginx.conf.new "$SITE"; then
        echo "nginx sin cambios"
    else
        cp "$SITE" "$SITE.prev"
        cp energyshark-nginx.conf.new "$SITE"
        if nginx -t; then
            systemctl reload nginx
            echo "nginx actualizado"
        else
            cp "$SITE.prev" "$SITE"
            echo "nginx -t falló: config anterior restaurada"
            exit 1
        fi
    fi
    rm -f energyshark-nginx.conf.new
fi

docker image prune -f
