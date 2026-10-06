#!/usr/bin/env bash
# Instala el agente de infraestructura de New Relic en la EC2 (G07, RNF05). Corre en el host,
# fuera de Docker, y reporta CPU, memoria, disco, red y los contenedores.
# Idempotente. Lee la license key de /opt/energyshark/.env, así no viaja por ningún comando.
# Uso: sudo bash newrelic-infra-setup.sh
set -euo pipefail

ENV_FILE=/opt/energyshark/.env

license_key=$(grep -E '^NEW_RELIC_LICENSE_KEY=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
if [ -z "$license_key" ]; then
  echo "Falta NEW_RELIC_LICENSE_KEY en $ENV_FILE" >&2
  exit 1
fi

# El archivo de configuración guarda la key: solo root puede leerlo.
install -m 600 /dev/null /etc/newrelic-infra.yml
cat > /etc/newrelic-infra.yml <<EOF
license_key: $license_key
display_name: energyshark-ec2
EOF

. /etc/os-release
# El keyring debe quedar legible para el usuario _apt; si no, apt ignora la firma del repo.
curl -fsSL https://download.newrelic.com/infrastructure_agent/gpg/newrelic-infra.gpg \
  | gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/newrelic-infra.gpg
chmod 644 /etc/apt/trusted.gpg.d/newrelic-infra.gpg
echo "deb https://download.newrelic.com/infrastructure_agent/linux/apt $VERSION_CODENAME main" \
  > /etc/apt/sources.list.d/newrelic-infra.list

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq newrelic-infra

systemctl enable --now newrelic-infra
systemctl restart newrelic-infra
systemctl is-active newrelic-infra
