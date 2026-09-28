# Infraestructura y deploy del backend

Runbook del rol de deploy (E). Explica qué hay en AWS, por qué, y cómo se configura desde cero.

## Cómo funciona el deploy

```
push a main ─→ GitHub Actions (.github/workflows/deploy.yml)
                 1. ruff + pytest
                 2. build de la imagen ─→ push a ECR (tags: <sha del commit> y latest)
                 3. SSM ─→ EC2: copia deploy/docker-compose.prod.yml a /opt/energyshark,
                                escribe el tag en release.env, docker compose pull + up --wait
```

- La EC2 **nunca** construye imágenes ni necesita acceso a GitHub (RNF04).
- GitHub entra a AWS con **OIDC**: credenciales temporales, sin access keys guardadas en el repo.
- La EC2 descarga desde ECR y recibe comandos de SSM con su **rol IAM**; tampoco guarda credenciales.
- Si falta configurar las variables del repo, el workflow corre los tests y se salta el deploy.

## Supuestos

- Cuenta AWS y EC2 reutilizadas de la E0: `t3.micro`, Ubuntu 24.04, región `us-east-2`.
- Repositorio ECR: `energyshark-backend`.
- En los archivos de `deploy/iam/`, reemplazar `ACCOUNT_ID`, `REGION` e `INSTANCE_ID`.

## Configuración (una sola vez)

### 1. Budget alerts (G08)

*Billing and Cost Management → Budgets → Create budget → Use a template → Zero spend budget*
(o un *Monthly cost budget* de pocos dólares), con los correos del grupo.

### 2. Repositorio ECR

*ECR → Private registry → Repositories → Create repository*

- Nombre: `energyshark-backend`
- Opcional: una *lifecycle policy* que conserve las últimas 10 imágenes, para no pasar del free tier
  de almacenamiento.

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
  (push al repo ECR + enviar el comando de deploy solo a nuestra instancia).

### 5. Rol de la EC2 (`energyshark-ec2`)

*IAM → Roles → Create role → AWS service → EC2*, con las políticas administradas:

- `AmazonEC2ContainerRegistryReadOnly`: descargar imágenes de ECR.
- `AmazonSSMManagedInstanceCore`: recibir los comandos de deploy.

Asignarlo a la instancia: *EC2 → Instances → (instancia) → Actions → Security → Modify IAM role*.
No requiere reiniciar. Verificar en *Systems Manager → Fleet Manager* que la instancia aparece como
*Online* (puede tardar unos minutos).

### 6. Preparar la EC2

Copiar y ejecutar el script de setup (es idempotente):

```bash
scp -i <clave>.pem deploy/ec2-setup.sh ubuntu@<ip>:/tmp/
ssh -i <clave>.pem ubuntu@<ip> 'sudo bash /tmp/ec2-setup.sh'
```

Crear el `.env` de producción a partir de [`.env.example`](../.env.example), con contraseñas reales:

```bash
ssh -i <clave>.pem ubuntu@<ip>
sudo nano /opt/energyshark/.env
sudo chmod 640 /opt/energyshark/.env && sudo chown root:docker /opt/energyshark/.env
```

**Si es la EC2 de la E0**, bajar ese stack para liberar memoria (el volumen se conserva salvo que
se agregue `-v`):

```bash
cd ~/EnergyShark && docker compose down
```

El stack de la E1 usa otro nombre de proyecto (`energyshark-e1`) y el puerto `127.0.0.1:8000`, así
que no choca con los restos de la E0. Nginx y certbot de la E0 pueden quedar instalados; su rol en
la E1 se decide al configurar API Gateway.

### 7. Variables del repo en GitHub

*G5ArquiSis/backend → Settings → Secrets and variables → Actions → Variables* (no son secretos):

| Variable | Valor |
|---|---|
| `AWS_REGION` | `us-east-2` |
| `AWS_ROLE_ARN` | ARN del rol del paso 4 |
| `ECR_REPOSITORY` | `energyshark-backend` |
| `EC2_INSTANCE_ID` | ID de la instancia (`i-...`) |

### 8. Primer deploy y verificación

*Actions → CI / Deploy backend → Run workflow* sobre `main`. Luego, en la EC2:

```bash
cd /opt/energyshark
sudo docker compose --env-file .env --env-file release.env -f docker-compose.prod.yml ps
curl -i http://127.0.0.1:8000/health
```

## Operación

| Tarea | Cómo |
|---|---|
| Desplegar | Merge a `main` |
| Volver a una versión anterior | `git revert` + merge a `main`; en una emergencia, editar `release.env` en la EC2 con el tag anterior y correr `up -d` |
| Ver logs | `sudo docker compose ... logs -f api` en `/opt/energyshark` |
| Agregar una variable de entorno | Documentarla en `.env.example` (PR) y agregarla a mano en `/opt/energyshark/.env` **antes** del merge |

## Pendiente

- Frontend: bucket S3 + CloudFront + rol OIDC para `G5ArquiSis/frontend` (RNF03).
- API Gateway con subdominio, CORS y autorizador JWT (RNF01, RNF02).
- New Relic APM + infraestructura (G07, RNF05).
- Cerrar el puerto 22 del security group cuando todo se opere por SSM.
