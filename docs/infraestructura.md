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
                                docker compose pull + up de a un servicio (ver abajo),
                                instala deploy/nginx/energyshark-tls.conf si cambió (nginx -t + reload)

frontend ─→ API Gateway (api.melchort.me, CORS) ─┐
                                                 ▼
internet ─→ Nginx en el host (TLS, melchort.me) ─→ 127.0.0.1:8001 master-1 ─┐
                                     (least_conn) ─→ 127.0.0.1:8002 master-2 ─┴→ postgres
     broker del curso ─→ connector ─→ POST http://master:8000/events (alias de ambas réplicas)
```

## URLs de producción

| URL | Qué es |
|---|---|
| https://api.melchort.me | La API, detrás de API Gateway (RNF01). Es la que usa el frontend |
| https://melchort.me | El mismo backend, directo por Nginx. Se mantiene por la E0 y es el origen del gateway |
| https://app.melchort.me | El frontend (ver el runbook del repo `frontend`) |

- La EC2 **nunca** construye imágenes ni necesita acceso a GitHub (RNF04).
- **Sin corte de servicio:** las réplicas de `master` se actualizan de a una, y el deploy espera a
  que cada una quede `healthy` antes de tocar la otra. Mientras una reinicia, Nginx envía el
  tráfico a la otra. `connector` sí se reinicia en cada deploy de código.
- **Un merge que solo toca `docs/` o archivos `.md` no despliega:** no cambia las imágenes.
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

### 10. API Gateway con subdominio propio (RNF01)

HTTP API `energyshark-api` en `us-east-2`, delante de Nginx. Se eligió HTTP API y no REST porque
trae CORS y autorizador JWT nativos, sin Lambdas. Los comandos exactos están en
[`deploy/api-gateway/crear.sh`](../deploy/api-gateway/crear.sh).

| Pieza | Configuración |
|---|---|
| Integración | `HTTP_PROXY` hacia `https://melchort.me/{proxy}` |
| Rutas | `GET`, `POST`, `PUT`, `PATCH` y `DELETE` sobre `/{proxy+}` |
| Stage | `$default`, con auto-deploy y límite de 50 requests por segundo (ráfaga de 100) |
| CORS | Orígenes `https://app.melchort.me`, `https://d1fglzovmxjg55.cloudfront.net` y `http://localhost:5173`; headers `Authorization` y `Content-Type` |
| Dominio | `api.melchort.me`, regional, TLS 1.2 como mínimo, certificado de ACM en `us-east-2` |

Tres cosas que no son obvias:

- **No usar una ruta `ANY`.** Con `ANY /{proxy+}` el preflight `OPTIONS` se reenvía al backend, que
  responde 405, y el navegador bloquea la llamada. Con rutas por método, `OPTIONS` no calza con
  ninguna y lo responde el gateway con 204.
- **El CORS vive solo en el gateway.** El backend no debe agregar `CORSMiddleware`: con el header
  duplicado el navegador rechaza la respuesta.
- **Un origen nuevo se agrega en el gateway**, no en el código (`aws apigatewayv2 update-api
  --cors-configuration ...`).

El dominio se configura en dos pasos, ambos con registros CNAME en el proveedor de DNS (Namecheap):

1. Pedir el certificado en ACM con validación por DNS y agregar el CNAME de validación que entrega.
   Ese registro no se borra: ACM lo usa para renovar.
2. Crear el *custom domain*, mapearlo al stage y agregar el CNAME `api` hacia el dominio regional
   que entrega API Gateway (`d-xxxx.execute-api.us-east-2.amazonaws.com`).

Verificación:

```bash
curl -i https://api.melchort.me/health
curl -i -X OPTIONS https://api.melchort.me/history \
  -H 'Origin: https://app.melchort.me' -H 'Access-Control-Request-Method: GET'
# 204 con access-control-allow-origin: https://app.melchort.me
```

### 11. Autorizador JWT de Auth0 (RNF02)

[`deploy/api-gateway/autorizador.sh`](../deploy/api-gateway/autorizador.sh) crea en la HTTP API un
autorizador JWT que valida el token contra Auth0 (firma, issuer, audience y expiración) antes de
pasar la request al backend. Necesita dos datos del tenant:

```bash
AUTH0_DOMAIN=xxxx.us.auth0.com AUTH0_AUDIENCE=https://api.melchort.me \
  deploy/api-gateway/autorizador.sh
```

- Quedan protegidas todas las rutas `/{proxy+}`. Solo `GET /health` es pública.
- El preflight de CORS (`OPTIONS`) no lleva token y lo sigue respondiendo el gateway.
- `https://melchort.me` no pasa por el gateway y sigue público para las rutas de la E0
  (`/history`, `/health`).
- **Las rutas de la E1 no se pueden llamar saltándose el gateway.** El gateway agrega a cada
  request el header `x-gateway-secret`, y Nginx responde 403 en `/negotiations` y `/cycles` si no
  coincide. `/internal/` responde 404 desde afuera: solo lo usa `connector`, por la red de Docker.

El secreto no está en el repo. Vive en dos lugares y se crea una sola vez:

```bash
SECRETO=$(openssl rand -hex 24)
# En la EC2 (Nginx lo lee con un include; sin este archivo, `nginx -t` falla):
echo "set \$gateway_secret \"$SECRETO\";" | sudo tee /etc/nginx/energyshark-gateway-secret.conf
sudo chmod 600 /etc/nginx/energyshark-gateway-secret.conf
# En el gateway, sobre la integración de /{proxy+}:
aws apigatewayv2 update-integration --api-id <api> --integration-id <integración> \
  --request-parameters "{\"overwrite:header.x-gateway-secret\":\"$SECRETO\"}"
```
- Para quitar la autenticación: `SIN_AUTH=1 deploy/api-gateway/autorizador.sh`.

Verificación:

```bash
curl -i https://api.melchort.me/health      # 200, sin token
curl -i https://api.melchort.me/history     # 401, sin token
curl -i https://api.melchort.me/history -H "Authorization: Bearer <token>"   # 200
```

## Operación

| Tarea | Cómo |
|---|---|
| Desplegar | Merge a `main` |
| Volver a una versión anterior | `git revert` + merge a `main`; en una emergencia, editar `release.env` en la EC2 con los tags anteriores y correr `up -d` |
| Ver logs | `sudo docker compose ... logs -f master-1 master-2` (o `connector`) en `/opt/energyshark` |
| Agregar una variable de entorno | Documentarla en `.env.example` (PR) y agregarla a mano en `/opt/energyshark/.env` **antes** del merge |
| Cambiar la config de Nginx | Editar `deploy/nginx/energyshark-tls.conf` (PR); el deploy la instala |
| Agregar un endpoint | Nada en el gateway: las rutas `/{proxy+}` lo cubren |
| Permitir un origen nuevo en CORS | `aws apigatewayv2 update-api --cors-configuration ...` y actualizar `deploy/api-gateway/crear.sh` (PR) |

## Pendiente

- Cerrar el puerto 22 del security group cuando todo se opere por SSM.
