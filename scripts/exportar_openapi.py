"""Exporta el contrato HTTP público de master a `contratos/openapi/openapi.yaml`.

La fuente de verdad son los routers y DTOs de FastAPI: este script toma `app.openapi()`,
quita lo que no es para la interfaz (`/internal/*`, que Nginx bloquea, y `/events`, la
ingesta de connector heredada de la E0) y agrega lo que FastAPI no infiere: servidores,
autenticación JWT y las respuestas 401/404/409.

Uso, desde `backend/` con el venv activo:

    python scripts/exportar_openapi.py ../contratos/openapi/openapi.yaml

No se conecta a la base: solo importa la app.
"""

import copy
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master"))
# El engine se crea al importar; con una URL ficticia basta porque nunca se conecta.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://contrato:contrato@localhost:1/contrato")

from app.main import app  # noqa: E402

PRIVATE_PREFIXES = ("/internal/", "/events")

SUMMARIES = {
    ("/health", "get"): "Estado del servicio",
    ("/history", "get"): "Historial de demand-set de la E0, paginado y filtrable",
    ("/history/{event_id}", "get"): "Detalle de un evento demand-set de la E0",
    ("/negotiations", "post"): "Retirado en el protocolo v3: siempre responde 410",
    ("/negotiations", "get"): "Historial de negociaciones (RF04)",
    ("/negotiations/{negotiation_id}", "get"): "Detalle de una negociación (RF04)",
    ("/cycles", "get"): "Ciclos con su balance y el presupuesto actual (RF01)",
    ("/cycles/{cycle_id}", "get"): "Historial completo de un ciclo (RF01)",
    ("/connectivity", "get"): "Distance-table vigente (RF02)",
    ("/message-log", "get"): "Mensajes duplicados, descartados o con NACK (RF05)",
    ("/wallet/balance-status", "get"): "Balance oficial de la wallet y su conciliación (AD5)",
    ("/strategy", "get"): "Consultar la estrategia activa (RF04)",
    ("/strategy", "put"): "Actualizar la estrategia para el siguiente ciclo (RF04)",
    ("/strategy/offer", "get"): "Consultar la configuración de la oferta propia (RF04)",
    ("/strategy/offer", "patch"): "Actualizar precio y cantidad de la oferta (RF04)",
    ("/strategy/swaps", "get"): "Consultar los swaps propios en curso (RF04)",
}
PUBLIC_WITHOUT_TOKEN = {"/health"}

DESCRIPTION = """\
API HTTP pública del nodo de Talca (TAL). La interfaz la consume solo a través de API
Gateway (`https://api.melchort.me`), que exige un JWT de Auth0 y agrega los headers CORS.

- RF01: `/cycles` y `/cycles/{cycle_id}`
- RF02: `/connectivity`
- RF04: `/strategy`, `/strategy/offer` y `/strategy/swaps`
- E1: `/negotiations` (POST retirado en protocolo v3)
- AD5: `/wallet/balance-status` (saldo oficial y conciliación de Wallet)
- RF05: `/message-log`
- E0: `/history`

Los endpoints `/internal/*` (contrato entre connector y master) quedan fuera de este archivo:
están documentados en `backend/docs/ciclo-negociacion.md` y Nginx los bloquea.

Archivo generado con `backend/scripts/exportar_openapi.py`; no se edita a mano.
"""

UNAUTHORIZED = {"description": "Falta el token de Auth0 o no es válido (lo responde API Gateway)"}

NOT_FOUND = {
    "/cycles/{cycle_id}": "Ciclo no encontrado",
    "/negotiations/{negotiation_id}": "Negociación no encontrada",
    "/history/{event_id}": "Evento no encontrado",
}

ERROR_SCHEMA = {
    "title": "ErrorDetail",
    "type": "object",
    "required": ["detail"],
    "properties": {"detail": {"type": "string"}},
}


def build_spec() -> dict:
    spec = app.openapi()

    spec["info"] = {"title": "EnergyShark E2 API", "version": "2.0.0", "description": DESCRIPTION}
    spec["servers"] = [
        {"url": "https://api.melchort.me", "description": "Producción (API Gateway)"},
        {"url": "http://127.0.0.1:8001", "description": "Local (master-1, sin JWT)"},
    ]
    spec["security"] = [{"bearerAuth": []}]

    components = spec["components"]
    components["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    components["schemas"]["ErrorDetail"] = ERROR_SCHEMA

    spec["paths"] = {
        path: operations
        for path, operations in spec["paths"].items()
        if not path.startswith(PRIVATE_PREFIXES)
    }

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if (path, method) not in SUMMARIES:
                sys.exit(
                    f"{method.upper()} {path} no tiene resumen en SUMMARIES: agregarlo aquí, "
                    "y la ruta al regex protegido de deploy/nginx/*.conf"
                )
            operation["summary"] = SUMMARIES[(path, method)]
            responses = operation["responses"]
            for response in responses.values():
                if response.get("description") == "Successful Response":
                    response["description"] = "Respuesta correcta"
                elif response.get("description") == "Validation Error":
                    response["description"] = "Parámetros o cuerpo inválidos"
            if path in PUBLIC_WITHOUT_TOKEN:
                operation["security"] = []
            else:
                responses["401"] = copy.deepcopy(UNAUTHORIZED)
            if path in NOT_FOUND:
                responses["404"] = _json_response(NOT_FOUND[path], "ErrorDetail")
            if path == "/negotiations" and method == "post":
                # Retirado en v3 (negotiation-proposal ya no existe): no hay respuesta correcta.
                responses["410"] = _json_response(
                    "Siempre: negotiation-proposal está retirado en el protocolo v3",
                    "ErrorDetail",
                )

    _drop_unused_schemas(spec)
    return spec


def _json_response(description: str, schema: str) -> dict:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
    }


def _drop_unused_schemas(spec: dict) -> None:
    """Quita los DTOs que solo usaban las rutas filtradas (por ejemplo OutboxItem)."""
    schemas = spec["components"]["schemas"]
    used: set[str] = set()
    pending = [spec["paths"]]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in used:
                    used.add(name)
                    pending.append(schemas[name])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)

    for name in list(schemas):
        if name not in used:
            del schemas[name]


class _Dumper(yaml.SafeDumper):
    """Strings de varias líneas como bloque literal y sin anclas YAML (`&id001`)."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Uso: python scripts/exportar_openapi.py <ruta de salida .yaml>")

    output = Path(sys.argv[1])
    output.write_text(
        yaml.dump(build_spec(), Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"Contrato escrito en {output}")


if __name__ == "__main__":
    main()
