# Infraestructura y deploy del backend

Runbook del rol de deploy (E). Explica qué hay en AWS, por qué, y cómo se configura desde cero.

## Cómo funciona el deploy

```
push a main ─→ GitHub Actions (.github/workflows/deploy.yml)
                 1. ruff + pytest (con un Postgres de servicio: los tests de integración corren)
                 2. build de energyshark-master y energyshark-connector ─→ push a ECR
                    (tags: <sha del commit> y latest)
                 3. SSM ─→ EC2: copia deploy/docker-compose.prod.yml a /opt/energyshark,
                                escribe MASTER_IMAGE y CONNECTOR_IMAGE en release.env,
                                docker compose pull + up --remove-orphans --wait,
                                instala deploy/nginx/energyshark-tls.conf si cambió (nginx -t + reload)

internet ─→ Nginx en el host (TLS, melchort.me) ─→ 127.0.0.1:8001 master-1 ─┐
                                     (least_conn) ─→ 127.0.0.1:8002 master-2 ─┴→ postgres
     broker del curso ─→ connector ─→ POST http://master:8000/events (alias de ambas réplicas)
```

- La EC2 **nunca** construye imágenes ni necesita acceso a GitHub (RNF04).
- GitHub entra a AWS con **OIDC**: credenciales temporales, sin access keys guardadas en el repo.
- La EC2 descarga desde ECR y recibe comandos de SSM con su **rol IAM**; tampoco guarda credenciales.
- Si falta configurar las variables del repo, el workflow corre los tests y se salta el deploy.
- Todo lo de la E0 sigue vigente en la E1: contenedores `master` y `connector` con HEALTHCHECK,
  Nginx en el host, `/history` paginado y filtrable, y las dos partes variables (HTTPS con
  renovación automática y balanceo de carga sobre dos réplicas de `master`).

## Supuestos

- Cuenta AWS y EC2 reutilizadas de la E0: `t3.micro`, Ubuntu 24.04, región `us-east-2`.
- Repositorios ECR: `energyshark-master` y `energyshark-connector`.
- En los archivos de `deploy/iam/`, reemplazar `ACCOUNT_ID`, `REGION` e `INSTANCE_ID`.

## Configuración (una sola vez)

### 1. Budget alerts (G08)

*Billing and Cost Management → Budgets → Create budget → Use a template → Zero spend budget*
(o un *Monthly cost budget* de pocos dólares), con los correos del grupo.

### 2. Repositorios ECR

*ECR → Private registry → Repositories → Create repository*, uno por servicio:

- `energyshark-master`
- `energyshark-connector`

Opcional: una *lifecycle policy* que conserve las últimas 10 imágenes de cada uno, para no pasar
del free tier de almacenamiento.

### 3. Proveedor OIDC de GitHub

*IAM → Identity providers → Add provider*

- Tipo: *OpenID Connect*
- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

### 4. Rol del CI (`energyshark-ci-backend`)

*IAM → Roles → Create role → Custom trust policy*

- Trust policy: [`deploy/iam/ci-backend-trust.json`](../deploy/iam/ci-backend-trust.json). Solo
  permite asumir el rol a workflows de `G5ArquiSis/backend` corriendo sobre `main`.
  GitHub usa un `sub` **inmutable**, con los IDs numéricos de la organización y del repo
  (`repo:G5ArquiSis@<id org>/backend@<id repo>:ref:refs/heads/main`). El prefijo exacto se obtiene con
  `gh api repos/G5ArquiSis/<repo>/actions/oidc/customization/sub`; con el formato antiguo
  (`repo:G5ArquiSis/backend:...`) AWS rechaza el token.
- Permisos: crear una política inline con [`deploy/iam/ci-backend-policy.json`](../deploy/iam/ci-backend-policy.json)
  (push a los dos repos ECR + enviar el comando de deploy solo a nuestra instancia).

### 5. Rol de la EC2 (`energyshark-ec2`)

*IAM → Roles → Create role → AWS service → EC2*, con las políticas administradas:

- `AmazonEC2ContainerRegistryReadOnly`: descargar imágenes de ECR.
- `AmazonSSMManagedInstanceCore`: recibir los comandos de deploy.

Asignarlo a la instancia: *EC2 → Instances → (instancia) → Actions → Security → Modify IAM role*.
No requiere reiniciar. Verificar en *Systems Manager → Fleet Manager* que la instancia aparece como
*Online* (puede tardar unos minutos).

### 6. Preparar la EC2

Ejecutar el script de setup (es idempotente; instala Docker si falta, swap de 1 GB, credential
helper de ECR y `/opt/energyshark`). Por SSM o por SSH:

```bash
scp -i <clave>.pem deploy/ec2-setup.sh ubuntu@<ip>:/tmp/
ssh -i <clave>.pem ubuntu@<ip> 'sudo bash /tmp/ec2-setup.sh'
```

Crear el `.env` de producción a partir de [`.env.example`](../.env.example), con contraseñas reales
y las credenciales del observer del broker:

```bash
sudo nano /opt/energyshark/.env
sudo chmod 640 /opt/energyshark/.env && sudo chown root:docker /opt/energyshark/.env
```

`DATABASE_URL` usa el host `postgres` (nombre del servicio en el compose).

### 7. Variables del repo en GitHub

*G5ArquiSis/backend → Settings → Secrets and variables → Actions → Variables* (no son secretos):

| Variable | Valor |
|---|---|
| `AWS_REGION` | `us-east-2` |
| `AWS_ROLE_ARN` | ARN del rol del paso 4 |
| `EC2_INSTANCE_ID` | ID de la instancia (`i-...`) |

### 8. Primer deploy y verificación

*Actions → CI / Deploy backend → Run workflow* sobre `main`. Luego, en la EC2:

```bash
cd /opt/energyshark
sudo docker compose --env-file .env --env-file release.env -f docker-compose.prod.yml ps
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8002/health
```

Los cuatro contenedores (`postgres`, `master-1`, `master-2`, `connector`) deben quedar `(healthy)`.

### 9. Nginx en el host (RNF3 de la E0)

Nginx corre en la EC2, fuera de Docker, termina TLS con el certificado de Let's Encrypt de
`melchort.me` y balancea (`least_conn`) entre `master-1` (`127.0.0.1:8001`) y `master-2`
(`127.0.0.1:8002`). Configs en
[`deploy/nginx/`](../deploy/nginx/): `energyshark.conf` es solo HTTP (bootstrap, antes de que
exista el certificado) y `energyshark-tls.conf` la final. Ambas van a la **misma** ruta,
`/etc/nginx/sites-available/energyshark`; nunca enlazar las dos en `sites-enabled` (declaran el
mismo `upstream`).

El deploy instala `energyshark-tls.conf` automáticamente cuando cambia: valida con `nginx -t`,
recarga, y si la validación falla restaura la config anterior (`energyshark.prev`) y marca el
deploy como fallido. A mano:

```bash
sudo cp deploy/nginx/energyshark-tls.conf /etc/nginx/sites-available/energyshark
sudo nginx -t && sudo systemctl reload nginx
```

Renovación: [`deploy/certbot/certbot-renew.cron`](../deploy/certbot/certbot-renew.cron) en
`/etc/cron.d/certbot-renew` (chequeo 00:00 y 12:00 UTC; `certbot.timer` desactivado a propósito).

## Operación

| Tarea | Cómo |
|---|---|
| Desplegar | Merge a `main` |
| Volver a una versión anterior | `git revert` + merge a `main`; en una emergencia, editar `release.env` en la EC2 con los tags anteriores y correr `up -d` |
| Ver logs | `sudo docker compose ... logs -f master-1 master-2` (o `connector`) en `/opt/energyshark` |
| Agregar una variable de entorno | Documentarla en `.env.example` (PR) y agregarla a mano en `/opt/energyshark/.env` **antes** del merge |
| Cambiar la config de Nginx | Editar `deploy/nginx/energyshark-tls.conf` (PR); el deploy la instala |

## Pendiente

- API Gateway con subdominio, CORS y autorizador JWT (RNF01, RNF02); su integración apunta a
  `https://melchort.me`.
- New Relic APM + infraestructura (G07, RNF05).
- Cerrar el puerto 22 del security group cuando todo se opere por SSM.
