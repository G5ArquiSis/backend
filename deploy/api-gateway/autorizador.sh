#!/usr/bin/env bash
# Agrega a la HTTP API el autorizador JWT de Auth0 (RNF02): API Gateway valida el token antes
# de pasar la request al backend. Se corre a mano, con credenciales de escritura en la cuenta.
# Es idempotente: si el autorizador ya existe, lo actualiza.
#
# Deja protegidas todas las rutas /{proxy+} y pública solo GET /health. El preflight de CORS
# (OPTIONS) no calza con ninguna ruta y lo sigue respondiendo el gateway, sin token.
#
# Uso:
#   AUTH0_DOMAIN=xxxx.us.auth0.com AUTH0_AUDIENCE=https://api.melchort.me ./autorizador.sh
# Para quitar la autenticación de las rutas: SIN_AUTH=1 ./autorizador.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
NOMBRE_API="${NOMBRE_API:-energyshark-api}"
NOMBRE_AUTORIZADOR="energyshark-auth0"
ORIGEN_BACKEND="${ORIGEN_BACKEND:-https://melchort.me}"
METODOS="GET POST PUT PATCH DELETE"

apigw() { aws apigatewayv2 --region "$REGION" "$@"; }

api_id=$(apigw get-apis --query "Items[?Name=='$NOMBRE_API'].ApiId | [0]" --output text)
[ "$api_id" != "None" ] || { echo "No existe la API $NOMBRE_API" >&2; exit 1; }

id_ruta() { apigw get-routes --api-id "$api_id" --query "Items[?RouteKey=='$1'].RouteId | [0]" --output text; }

if [ -n "${SIN_AUTH:-}" ]; then
  for metodo in $METODOS; do
    apigw update-route --api-id "$api_id" --route-id "$(id_ruta "$metodo /{proxy+}")" \
      --authorization-type NONE >/dev/null
  done
  echo "Rutas sin autenticación."
  exit 0
fi

AUTH0_DOMAIN="${AUTH0_DOMAIN:?dominio del tenant, sin https://}"
AUTH0_AUDIENCE="${AUTH0_AUDIENCE:?identifier de la API en Auth0}"
# El issuer debe coincidir con el claim iss del token: lleva la barra final.
jwt_config="Audience=$AUTH0_AUDIENCE,Issuer=https://$AUTH0_DOMAIN/"

autorizador=$(apigw get-authorizers --api-id "$api_id" \
  --query "Items[?Name=='$NOMBRE_AUTORIZADOR'].AuthorizerId | [0]" --output text)
if [ "$autorizador" = "None" ]; then
  autorizador=$(apigw create-authorizer --api-id "$api_id" --name "$NOMBRE_AUTORIZADOR" \
    --authorizer-type JWT --identity-source '$request.header.Authorization' \
    --jwt-configuration "$jwt_config" --query AuthorizerId --output text)
else
  apigw update-authorizer --api-id "$api_id" --authorizer-id "$autorizador" \
    --jwt-configuration "$jwt_config" >/dev/null
fi

# /health queda pública: la ruta exacta gana a /{proxy+}. Necesita su propia integración
# porque la general arma la URL con el parámetro {proxy}, que acá no existe.
if [ "$(id_ruta 'GET /health')" = "None" ]; then
  integracion_health=$(apigw create-integration --api-id "$api_id" \
    --integration-type HTTP_PROXY --integration-method GET \
    --integration-uri "$ORIGEN_BACKEND/health" --payload-format-version 1.0 \
    --query IntegrationId --output text)
  apigw create-route --api-id "$api_id" --route-key 'GET /health' \
    --target "integrations/$integracion_health" >/dev/null
fi

for metodo in $METODOS; do
  apigw update-route --api-id "$api_id" --route-id "$(id_ruta "$metodo /{proxy+}")" \
    --authorization-type JWT --authorizer-id "$autorizador" >/dev/null
done

echo "Autorizador $autorizador aplicado a las rutas /{proxy+} de $api_id; GET /health queda pública."
