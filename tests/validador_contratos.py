"""Valida mensajes AMQP contra los schemas del curso y contra los del grupo (E4 de la E2).

Dos fuentes, cada una un directorio con `schemas/`:

- `ENERGYSHARK_CONTRACTS_DIR`: clon de `iic2173/energyshark-contracts` en el tag v3.0.2
  (`git clone --branch v3.0.2 https://github.com/iic2173/energyshark-contracts.git`).
  Manda sobre cualquier texto: es lo que la central acepta.
- `GROUP_CONTRACTS_DIR`: clon de `G5ArquiSis/contratos` (nuestros schemas, `schemas/*.schema.json`).

Si una variable no está, los tests que la necesitan se saltan; con `CONTRACTS_REQUIRED=1` (lo
que hace el CI) fallan en vez de saltarse, para que un clon que no se hizo no pase en verde.

El validador es `jsonschema` 2020-12 con los formatos `uuid` y `date-time` activos. El curso
sólo prueba Ajv: `tests/test_contratos.py` corre los ejemplos válidos e inválidos del curso con
este validador para comprobar que juzga igual.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

COURSE_ENV = "ENERGYSHARK_CONTRACTS_DIR"
GROUP_ENV = "GROUP_CONTRACTS_DIR"
COURSE_VERSION = "3.0.2"

# Formatos que usan los schemas. `date-time` en jsonschema necesita `rfc3339-validator`; sin
# él el formato se ignora en silencio, así que se exige al cargar (ver `_format_checker`).
_FORMATS = ("uuid", "date-time")


@dataclass(frozen=True)
class Contracts:
    """Schemas de una fuente, por ruta relativa a `schemas/` (`amqp/ack.schema.json`)."""

    root: Path
    schemas: dict[str, dict]
    registry: Registry

    def validator(self, name: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.schemas[name], registry=self.registry, format_checker=_format_checker()
        )

    def errors(self, name: str, instance) -> list[ValidationError]:
        return sorted(self.validator(name).iter_errors(instance), key=lambda e: list(e.path))

    def assert_valid(self, name: str, instance) -> None:
        errors = self.errors(name, instance)
        if errors:
            detail = "\n".join(f"  - {_where(e)}: {e.message}" for e in errors[:10])
            pytest.fail(
                f"{instance.get('type', '?')} no cumple {self.root.name}/schemas/{name}:\n"
                f"{detail}\nmensaje: {json.dumps(instance, ensure_ascii=False)}"
            )


def _where(error: ValidationError) -> str:
    return "/".join(str(p) for p in error.absolute_path) or "(raíz)"


@cache
def _format_checker() -> FormatChecker:
    checker = FormatChecker(_FORMATS)
    missing = [f for f in _FORMATS if f not in checker.checkers]
    if missing:
        raise RuntimeError(f"jsonschema sin soporte para los formatos {missing}")
    # Sin rfc3339-validator, `date-time` queda registrado pero acepta cualquier string.
    if checker.conforms("no es una fecha", "date-time"):
        raise RuntimeError("falta rfc3339-validator: el formato date-time no se valida")
    return checker


def _load(root: Path) -> Contracts:
    base = root / "schemas"
    schemas = {
        str(path.relative_to(base)): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(base.rglob("*.schema.json"))
    }
    if not schemas:
        raise RuntimeError(f"{base} no tiene schemas")
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Contracts(root=root, schemas=schemas, registry=registry)


def _source(env: str) -> Contracts:
    value = os.environ.get(env)
    if not value:
        message = f"{env} no está definida: sin schemas contra los que validar"
        if os.environ.get("CONTRACTS_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return _load(Path(value))


@cache
def _course() -> Contracts:
    contracts = _source(COURSE_ENV)
    package = json.loads((contracts.root / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != COURSE_VERSION:
        pytest.fail(f"{COURSE_ENV} es la versión {package.get('version')}, no {COURSE_VERSION}")
    return contracts


@cache
def _group() -> Contracts:
    return _source(GROUP_ENV)


def course() -> Contracts:
    return _course()


def group() -> Contracts:
    return _group()


def course_schema_for(message_type: str) -> str:
    return f"amqp/{message_type}.schema.json"


def group_schema_for(message_type: str) -> str:
    return f"{message_type}.schema.json"
