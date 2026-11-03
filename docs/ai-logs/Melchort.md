# AI log — Melchort

- **Integrante:** Melchor Guerrero
- **Autocompletado:** no

Todas las entradas usan Claude Code (modelo Claude Opus 5.5) en modo agéntico: la IA escribió los
archivos, ejecutó los comandos de AWS y GitHub y abrió los PRs. Los commits correspondientes llevan
`Co-Authored-By: Claude`.

## 2026-09-27 — Esqueleto de los repos y pipeline de deploy del backend

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** crear los repos del grupo y dejar un deploy automático del backend sobre la EC2 de la E0.
- **Prompts relevantes:**
  > "Ayudame a construir la infraestructura para comenzar a trabajar. Necesito hacer nuevos repos?"

  > "Debo configurar algo para conectar el deploy a los nuevos repos. Considera que mi deploy ya funcionaba en aws con el repo en el que estas actualmente"

  > "instalemos el cli de aws para que puedas configurar"
- **Qué produjo la IA:** la estructura inicial de `backend` y `contratos` (README, `docs/`,
  plantilla de ADR, `contratos/AGENTS.md`); `.github/workflows/deploy.yml` (tests, build, push a
  ECR y deploy por SSM); `deploy/docker-compose.prod.yml`, `deploy/ec2-setup.sh` y las políticas de
  `deploy/iam/`. En AWS: repos de ECR, roles de IAM con OIDC para el CI, rol de la instancia y la
  alerta de presupuesto.
- **Verificación:** el workflow corrió completo en GitHub Actions y los contenedores quedaron
  `healthy` en la EC2. El primer deploy falló con `Not authorized to perform
  sts:AssumeRoleWithWebIdentity`; la causa era el formato del `sub` de OIDC y se corrigió en `ad5c8a3`.
- **Correcciones del integrante:** indiqué que el repo de la E0 ya no se usaría y que la
  organización era `G5ArquiSis`; definí el stack (FastAPI + Postgres), el monto de la alerta
  (US$5) y que una sola persona se encarga del deploy.
- **Referencia:** commits `921bce7`, `37df641`, `80bfe6e`, `ad5c8a3`; en `contratos`, `e763acc`.

## 2026-09-28 — Portar el código de la E0 como base del backend

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico (skills de planificación y
  ejecución de superpowers).
- **Tarea:** que el backend de la E1 siga cumpliendo todos los requisitos de la E0.
- **Prompts relevantes:**
  > "revisa el enunciado de la entrega pasada @Enunciado_E0.pdf y verfica que no hayas roto nada"

  > "Mi repo anterior en el que te encuentras ahora cumplia con todos los requisitos de la E0"

  > "utiliza la skill superpowers para planificar y ejecutar todo esto"
- **Qué produjo la IA:** el plan en `docs/superpowers/plans/2026-09-28-portar-e0-a-e1.md`; la
  copia sin modificar de `master/`, `connector/` y `tests/` desde la E0; el deploy de dos imágenes
  (`energyshark-master` y `energyshark-connector`); las configuraciones de Nginx y certbot
  versionadas en `deploy/`; `docs/infraestructura.md`.
- **Verificación:** 37 tests pasan en CI con Postgres. En producción se comprobó `/health`,
  la paginación de `/history` (25 por defecto), los filtros, el detalle por id, la redirección
  de HTTP a HTTPS y que `connector` consume eventos reales del broker.
- **Correcciones del integrante:** la IA había reemplazado el código de la E0 por un esqueleto
  nuevo y había dicho que Nginx y certbot ya no hacían falta; corregí ambas cosas, porque las
  entregas son acumulativas.
- **Referencia:** commits `dab4622`, `f25370a`, `f02b747`, `0a0a51a`; en `contratos`, `93a31c5`.

## 2026-09-28 — Balanceo de carga con dos réplicas de master

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** recuperar la parte variable de la E0 que el port había dejado fuera.
- **Prompts relevantes:**
  > "Faltan cosas de la E0?"

  > "En teoria las entregas son crecientes por lo tanto todo lo que tiene una entrega lo debe tener la siguiente"

  > "Abre el pr"
- **Qué produjo la IA:** `master-1` y `master-2` en ambos compose, el `upstream` de Nginx con
  `least_conn` y el paso del deploy que instala la configuración de Nginx y la revierte si
  `nginx -t` falla.
- **Verificación:** CI en verde en el PR. Falta comprobar en producción después del merge.
- **Correcciones del integrante:** ninguna sobre el código; decidí que las partes variables de la
  E0 también se mantienen.
- **Referencia:** PR #1, commit `12190a7`.

## 2026-09-28 — Enlace al contexto compartido

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** cumplir RDOC04 enlazando `contratos/AGENTS.md` desde el README.
- **Prompts relevantes:**
  > "Haz los enlaces de los readme"
- **Qué produjo la IA:** el párrafo del README con el enlace.
- **Verificación:** la URL enlazada responde 200.
- **Correcciones del integrante:** ninguna.
- **Referencia:** PR #2, commit `7d1c0be`.

## 2026-10-05 — Formato de los AI logs

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** definir un formato de AI logs propio del grupo, porque el curso no entrega uno.
- **Prompts relevantes:**
  > "Como explicita el enunciado que se debe hacer el registro de uso de IA"

  > "No hay un template del curso. Cada grupo crea el suyo y debe ser consistente en el uso"

  > "Abre las branches y haz la propuesta pero no commitees nada"
- **Qué produjo la IA:** la sección "Registro de uso de IA" de `contratos/AGENTS.md`, la carpeta
  `docs/ai-logs/` con su plantilla y este archivo.
- **Verificación:** revisión de los enlaces entre documentos.
- **Correcciones del integrante:** <!-- completar al revisar la propuesta -->
- **Referencia:** rama `docs/formato-ai-logs` en los tres repos.

## 2026-10-06 — API Gateway y subdominios

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** poner la API detrás de API Gateway con subdominio propio y CORS (RNF01).
- **Prompts relevantes:**
  > "Que hay que hacer con api gateway"

  > "no deberia usar un subdominio para la api y otro para el frontend"

  > "Pide los dos certificados y pásame los registros"

  > "sigamos. Todo esto es requuerido por el enunciado?"

  > "Prepara la documentacion sin hacer commit"
- **Qué produjo la IA:** en AWS, el certificado de ACM, la HTTP API `energyshark-api` con su
  integración, rutas, CORS y límite de requests, y el dominio `api.melchort.me`. En el repo,
  `deploy/api-gateway/crear.sh` y la sección 10 de `docs/infraestructura.md`.
- **Verificación:** por `https://api.melchort.me`, `/health` y `/history` responden 200 y un id
  inexistente 404; el preflight de CORS responde 204 para los orígenes permitidos. La primera
  versión usaba una ruta `ANY` y el preflight devolvía 405; se corrigió con rutas por método.
- **Correcciones del integrante:** propuse el subdominio para el frontend, que la IA había dejado
  como opcional, y agregué a mano los cuatro registros DNS en Namecheap.
- **Referencia:** rama `docs/api-gateway-dominios`.

## 2026-10-06 — Monitoreo con New Relic

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** instalar monitoreo SaaS: APM en `master` y agente de infraestructura en la EC2 (G07,
  RNF05), y documentar cómo replicarlo (RDOC03).
- **Prompts relevantes:**
  > "Ya cree la cuenta de New Relic y tengo la key"

  > "Ya cargue la key, responde 1"

  > "Aparece el host. Ya subi la branch, abre el pr."
- **Qué produjo la IA:** `master/entrypoint.sh` y los cambios en el `Dockerfile` y
  `requirements.txt` de `master`; `deploy/newrelic-infra-setup.sh`; el paso del CI que construye la
  imagen de `master` y prueba que arranque bajo el agente; `docs/monitoreo.md`. En la EC2, la
  instalación del agente de infraestructura.
- **Verificación:** el agente de infraestructura quedó activo y el host aparece en New Relic. En
  el CI, la imagen de `master` responde `/health` bajo el agente con una key falsa. La primera
  versión del script de instalación falló por los permisos del keyring de apt y se corrigió. El
  APM en producción se verifica después del merge.
- **Correcciones del integrante:** creé la cuenta de New Relic y cargué la license key
  directamente en el `.env` de la EC2, sin pasarla por la IA ni por el repo; confirmé en el panel
  que el host reporta.
- **Referencia:** PR #5.

## 2026-10-07 — Integración de connector con el ciclo de negociación

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** dejar la rama de mensajería (rol A) funcionando sobre `main`: que `connector`
  entregue los mensajes de la central a `master` y publique lo que `master` tiene pendiente.
- **Prompts relevantes:**
  > "catalina corrigio su pr. Miralo antes de que hagamos el push de esta parte"

  > "Ya se acepto mi pr. Ahora trae la rama de catalina y corrigela. No hagas commit"
- **Qué produjo la IA:** la fusión de `main` en `feature/mensajeria_protocolo`, descartando lo
  que esa rama duplicaba en `master` (RF02 y RF05 ya estaban en `main`); la reescritura de
  `connector/consumer.py` y `connector/master_client.py` sobre la validación del envelope que
  ya existía en la rama; los tests de `connector`; las variables nuevas en `.env.example`.
- **Verificación:** 101 tests pasan contra un Postgres real. El código de `connector` se
  ejecutó contra un `master` real por HTTP, con un canal AMQP simulado: aplica, detecta el
  duplicado, responde NACK, registra el descarte y publica el reporte. No se probó contra el
  broker del curso: la conexión con `pika` y el destino de publicación quedan por confirmar.
- **Correcciones del integrante:** <!-- completar -->
- **Referencia:** PR #7.

## 2026-11-03 — Completar RF03: peticiones directas y sin NACK de respuestas

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** cerrar lo que faltaba de RF03 según la revisión del enunciado E1 contra el código y
  producción.
- **Prompts relevantes:**
  > "Quiero que hagas una revision punto por punto viendo que se cumple y que falta."

  > "Trabajemos sobre la branch del pr 18 a la espera de que lo acepten."

  > "primero corrijamos todo lo necesario para que la entrega 1 este completa"
- **Qué produjo la IA:** en `master`, `_request_missing_statements` en `cycle_repository.py`,
  que pide el `status-statement` (y la `distance-table` si no hay) cuando la central habla de un
  ciclo sin mandar su estado, con espera, intervalo y tope configurables; el ciclo guarda su
  `created_at` con el reloj de la aplicación. En `connector`, un `ack`, `nack` o `error` inválido
  se registra como descartado en vez de responderse con NACK. Tests de ambos casos,
  `scripts/demo_anomalia_1.py` adaptado de la versión de Vicente a `/internal/messages`, y la
  documentación en `docs/ciclo-negociacion.md` y `.env.example`.
- **Verificación:** 114 tests pasan contra un Postgres real (`ruff` limpio). El script de la
  anomalía 1 se corrió contra un `master` local: el reenvío responde `duplicate`, los balances no
  cambian y el duplicado aparece en `/message-log`.
- **Correcciones del integrante:** <!-- completar al revisar -->
- **Referencia:** rama `feat/rf03-request-y-no-nack`.

## 2026-11-03 — Contrato completo y documentación de arquitectura

- **Herramienta y modo:** Claude Code, Claude Opus 5.5, agéntico.
- **Tarea:** cerrar RDOC03 y RDOC04: diagrama de componentes, OpenAPI que coincida con la API
  real, schemas de los mensajes y milestones al día.
- **Prompts relevantes:**
  > "Quiero que hagas una revision punto por punto viendo que se cumple y que falta."

  > "primero corrijamos todo lo necesario para que la entrega 1 este completa"
- **Qué produjo la IA:**
  - `scripts/exportar_openapi.py`, que genera `contratos/openapi/openapi.yaml` desde FastAPI. El
    OpenAPI anterior describía campos que la API no devuelve.
  - Los 12 JSON Schemas en `contratos/schemas/`.
  - `docs/arquitectura.md` con el diagrama UML de componentes en Mermaid.
  - La actualización de `milestones.md`.
  - En el ADR-0001: el estado pasa a aceptado, la ruta `/interno/` se corrige a `/internal/` y se
    agrega una nota sobre la topología heredada de la E0.
- **Verificación:**
  - El OpenAPI pasa `openapi-spec-validator`.
  - Los schemas se validaron con `jsonschema` en 22 casos: los ejemplos del enunciado, los
    mensajes que arman `connector` y `master`, y casos inválidos.
  - También se validaron tres mensajes reales de producción.
  - `ruff check` sin errores.
- **Correcciones del integrante:** ninguna hasta ahora (pendiente de revisión en el PR).
- **Referencia:** rama `docs/arquitectura-y-contrato` (backend) y rama
  `docs/contrato-api-y-mensajes` (contratos).
