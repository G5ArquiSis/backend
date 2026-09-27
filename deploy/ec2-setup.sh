#!/bin/bash
# Prepara una EC2 Ubuntu 24.04 para recibir deploys desde ECR vía SSM.
#
# Idempotente: sirve como user data de una instancia nueva y también para correrlo a mano
# (con sudo) en una instancia que ya existe, por ejemplo la EC2 de la E0.
#
# Requiere que la instancia tenga el rol IAM descrito en docs/infraestructura.md
# (lectura de ECR + SSM). No guarda credenciales: el credential helper usa el rol.

set -euo pipefail
exec > >(tee -a /var/log/energyshark-setup.log) 2>&1

echo "=== Paquetes base ==="
apt-get update -y
apt-get install -y ca-certificates curl gnupg amazon-ecr-credential-helper

if ! command -v docker >/dev/null; then
    echo "=== Docker Engine + Compose plugin ==="
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    usermod -aG docker ubuntu
fi
systemctl enable --now docker

echo "=== Docker autentica contra ECR con el rol de la instancia ==="
# SSM ejecuta los deploys como root; ubuntu se configura para operar a mano.
for home in /root /home/ubuntu; do
    mkdir -p "$home/.docker"
    echo '{"credsStore": "ecr-login"}' > "$home/.docker/config.json"
done
chown -R ubuntu:ubuntu /home/ubuntu/.docker

echo "=== Directorio de la aplicación ==="
install -d -m 0750 -o root -g docker /opt/energyshark
if [ ! -f /opt/energyshark/.env ]; then
    echo "AVISO: falta /opt/energyshark/.env; crearlo a partir de .env.example antes del primer deploy."
fi

echo "=== Agente SSM (viene preinstalado en las AMI de Ubuntu) ==="
snap services amazon-ssm-agent || echo "AVISO: no se encontró amazon-ssm-agent"

echo "=== Listo ==="
