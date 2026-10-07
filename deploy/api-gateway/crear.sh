#!/usr/bin/env bash
# Crea la HTTP API de API Gateway delante del backend (RNF01). Se corre una sola vez, a mano,
# con credenciales de escritura en la cuenta; el deploy del repo no toca el gateway.
# Antes: el certificado de ACM para $DOMINIO debe estar emitido en la misma región.
# Después: agregar en el DNS el CNAME que imprime el script.
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
DOMINIO="${DOMINIO:-api.melchort.me}"
ORIGEN_BACKEND="${ORIGEN_BACKEND:-https://melchort.me}"
CERT_ARN="${CERT_ARN:?ARN del certificado de ACM para $DOMINIO}"
ORIGENES_CORS="${ORIGENES_CORS:-https://app.melchort.me,https://d1fglzovmxjg55.cloudfront.net,http://localhost:5173}"

api_id=$(aws apigatewayv2 create-api --region "$REGION" \
  --name energyshark-api --protocol-type HTTP \
  --cors-configuration "AllowOrigins=$ORIGENES_CORS,AllowMethods=GET,POST,PUT,PATCH,DELETE,OPTIONS,AllowHeaders=authorization,content-type,MaxAge=600" \
  --query ApiId --output text)

integracion=$(aws apigatewayv2 create-integration --region "$REGION" --api-id "$api_id" \
  --integration-type HTTP_PROXY --integration-method ANY \
  --integration-uri "$ORIGEN_BACKEND/{proxy}" \
  --payload-format-version 1.0 --timeout-in-millis 29000 \
  --query IntegrationId --output text)

# Una ruta por método y no ANY: así OPTIONS no calza con ninguna y el preflight de CORS lo
# responde el gateway (204) en vez de reenviarse al backend (405).
for metodo in GET POST PUT PATCH DELETE; do
  aws apigatewayv2 create-route --region "$REGION" --api-id "$api_id" \
    --route-key "$metodo /{proxy+}" --target "integrations/$integracion" >/dev/null
done

aws apigatewayv2 create-stage --region "$REGION" --api-id "$api_id" \
  --stage-name '$default' --auto-deploy \
  --default-route-settings 'ThrottlingBurstLimit=100,ThrottlingRateLimit=50' >/dev/null

destino=$(aws apigatewayv2 create-domain-name --region "$REGION" --domain-name "$DOMINIO" \
  --domain-name-configurations "CertificateArn=$CERT_ARN,EndpointType=REGIONAL,SecurityPolicy=TLS_1_2" \
  --query 'DomainNameConfigurations[0].ApiGatewayDomainName' --output text)

aws apigatewayv2 create-api-mapping --region "$REGION" --domain-name "$DOMINIO" \
  --api-id "$api_id" --stage '$default' >/dev/null

echo "API creada: $api_id"
echo "Agregar en el DNS: CNAME ${DOMINIO%%.*} -> $destino"
