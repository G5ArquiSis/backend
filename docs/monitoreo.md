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

## Métricas de la E2 (RNF05): mercado, wallet y ciclos

master registra tres eventos propios (`master/app/metrics.py`). Sin agente, no hace nada.

| Evento | Cuándo |
|---|---|
| `WalletCall` | Cada intento HTTP del `WalletClient`, reintentos incluidos. Es el hook por defecto del cliente |
| `InboundMessage` | Cada mensaje que connector entrega a master. `feed` vale `market`, `wallet` o `cycle`, y `lagMs` es el atraso desde el emisor |
| `CycleClosed` | Una vez por ciclo, al vencer su ventana: `owedReport`, `reportSent`, `reportError` |

**Dashboard:** *Dashboards → Import dashboard* con el contenido de
[`deploy/newrelic/dashboard.json`](../deploy/newrelic/dashboard.json). Trae cuatro páginas:
Ciclos, Mercado, Wallet e Infraestructura.

**Alertas** (*Alerts → Alert conditions → New NRQL condition*). Se crean **después** del deploy
que trae estos eventos, porque sin datos las de pérdida de señal se disparan:

Todas filtran por `appName = 'EnergyShark E1'` (el `NEW_RELIC_APP_NAME` de producción), para que
un ensayo local con la license key no las dispare.

| Alerta | NRQL | Umbral |
|---|---|---|
| Ciclo sin reporte (G03, guardián) | `SELECT count(*) FROM CycleClosed WHERE appName = 'EnergyShark E1' AND owedReport IS TRUE AND reportSent IS FALSE` | > 0 al menos una vez en 5 min |
| Reloj detenido (guardián) | `SELECT count(*) FROM CycleClosed WHERE appName = 'EnergyShark E1'` | Pérdida de señal tras 45 min (dos ciclos) |
| Connector no late (guardián) | `SELECT count(*) FROM ContainerSample WHERE name = 'energyshark-e1-connector-1' AND status NOT LIKE '%(healthy)%'` | > 0 en todas las ventanas durante 5 min. Además, pérdida de señal tras 10 min (el contenedor desapareció) |
| Feed del mercado detenido | `SELECT count(*) FROM InboundMessage WHERE feed = 'market'` | Pérdida de señal tras 10 min. Activarla sólo con `MARKET_ENABLED=true` |
| Wallet fallando | `SELECT percentage(count(*), WHERE outcome != 'ok') FROM WalletCall` | > 20 % durante 5 min |
| Wallet lenta | `SELECT percentile(durationMs, 95) FROM WalletCall` | > 5000 ms durante 5 min |

### Guardián de ciclos (E6)

master no tiene temporizadores: si connector deja de llamar a `/internal/outbox/claim`, no sale
el reporte y tampoco se registra `CycleClosed`. Por eso el guardián son tres alertas que se
cubren entre sí, evaluadas por New Relic (no dependen de nuestro stack):

- **Ciclo sin reporte:** el ciclo cerró, la central nos debía reporte (hubo status-statement)
  y no salió.
- **Reloj detenido:** no hay `CycleClosed` en dos ciclos. Cubre el caso en que el problema es
  justamente el reloj: connector caído, master caído o la base inaccesible.
- **Connector no late:** el healthcheck de connector (`heartbeat.py`: cada consumidor esperado
  late) deja de estar `healthy`. Llega antes que las otras dos, sin esperar al cierre del ciclo.

En `ContainerSample` no existe `healthStatus`: la salud de Docker viene en `status`, con la forma
`Up 53 minutes (healthy)`. Durante un deploy connector pasa unos segundos por
`(health: starting)`; por eso el umbral exige 5 minutos seguidos.

**Cómo probarlo sin tocar producción:** stack local con la license key y otro nombre de app
(`NEW_RELIC_APP_NAME=EnergyShark ensayo`), y una copia de las dos primeras condiciones con
`appName = 'EnergyShark ensayo'`. Después:

```bash
python3 scripts/ensayo_caida_contenedor.py --perder-reporte
```

El script mata master y connector antes del plazo del reporte y los levanta después de que la
ventana cerró: el ciclo queda sin reporte (`GET /cycles/{id}` con `reportSentAt` nulo) y
`CycleClosed` llega con `reportSent` false. La condición de prueba debe abrir un incidente en
≤ 5 min. Mientras están abajo, sin `CycleClosed` ni claims, el dashboard muestra el reloj
detenido. Borrar las condiciones de prueba al terminar.
