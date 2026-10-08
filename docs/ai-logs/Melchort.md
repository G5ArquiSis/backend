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
