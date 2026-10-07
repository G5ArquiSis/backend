# Monitoreo (G07, RNF05, RDOC03)

El sistema se monitorea con New Relic (cuenta gratuita del grupo). Hay dos agentes, y los dos
reportan a la misma cuenta:

| Agente | Dónde corre | Qué reporta |
|---|---|---|
| APM (Python) | Dentro de cada contenedor de `master` | Cada request HTTP, agrupada por endpoint: throughput, tiempo de respuesta, errores y consultas a Postgres |
| Infraestructura | En el host de la EC2, fuera de Docker | CPU, memoria, disco y red de la instancia, y el estado y consumo de cada contenedor |

`connector` no lleva APM: no atiende requests HTTP. Su actividad se ve como requests
`POST /events` en `master` y como contenedor en el agente de infraestructura.

## Cómo está instalado

**APM.** El paquete `newrelic` está en [`master/requirements.txt`](../master/requirements.txt).
[`master/entrypoint.sh`](../master/entrypoint.sh) arranca el servicio con
`newrelic-admin run-program` solo si existe `NEW_RELIC_LICENSE_KEY`; sin ella (desarrollo local,
CI) corre igual, sin agente. El agente instrumenta FastAPI y SQLAlchemy por sí solo: no hay
código de New Relic en la aplicación.

**Infraestructura.** [`deploy/newrelic-infra-setup.sh`](../deploy/newrelic-infra-setup.sh) instala
el paquete `newrelic-infra` desde el repositorio apt de New Relic y lo deja como servicio de
systemd. Lee la license key del `.env` de producción.

## Cómo replicarlo desde cero

1. Crear una cuenta en https://newrelic.com/signup (el plan gratuito alcanza y no pide tarjeta).
2. En New Relic, *API Keys*, copiar la clave de tipo *License* (ingest).
3. En la EC2, agregar a `/opt/energyshark/.env` las variables documentadas en
   [`.env.example`](../.env.example). El comando lee la key sin mostrarla ni dejarla en el
   historial:

   ```bash
   sudo bash -c 'read -rsp "License key: " K && echo && printf "NEW_RELIC_LICENSE_KEY=%s\nNEW_RELIC_APP_NAME=EnergyShark E1\nNEW_RELIC_LOG=stderr\n" "$K" >> /opt/energyshark/.env'
   ```

4. Instalar el agente de infraestructura:

   ```bash
   sudo bash deploy/newrelic-infra-setup.sh
   systemctl is-active newrelic-infra     # active
   ```

5. Desplegar `master` (merge a `main`). Al arrancar con la key en el entorno, cada réplica se
   registra en New Relic con el nombre de `NEW_RELIC_APP_NAME`.
6. Generar tráfico y esperar uno o dos minutos:

   ```bash
   for i in $(seq 1 30); do curl -s -o /dev/null https://api.melchort.me/history; done
   ```

La license key nunca se sube al repo ni se comparte por chat: vive solo en el `.env` de la EC2 y
en `/etc/newrelic-infra.yml` (legible solo por root).

## Dónde mirar

| Qué se quiere ver | Dónde, en New Relic |
|---|---|
| Requests por endpoint (RNF05) | *APM & Services → EnergyShark E1 → Transactions* |
| Errores de la API | *APM & Services → EnergyShark E1 → Errors* |
| Consultas a Postgres | *APM & Services → EnergyShark E1 → Databases* |
| CPU, memoria y disco de la instancia | *Infrastructure → Hosts → energyshark-ec2* |
| Contenedores (estado, reinicios, memoria) | *Infrastructure → Hosts → energyshark-ec2 → Containers* |

## Verificación sin entrar a New Relic

```bash
# En la EC2: el agente de infraestructura está corriendo y conectado.
systemctl is-active newrelic-infra
sudo journalctl -u newrelic-infra -n 20 --no-pager | grep "connect got id"

# El agente de APM está activo en una réplica.
sudo docker logs energyshark-e1-master-1-1 2>&1 | grep -i "new relic" | tail -3
```

En el log del agente de infraestructura aparece el error `failed to load log configs` del
`log-forwarder`. Es esperado: no se configuró el reenvío de logs, que no se necesita.
