"""Fallas inyectables para probar cómo reacciona el nodo ante una wallet que falla.

Se activan con `POST /_fake/faults` y cada una se consume en las próximas `times`
peticiones (o eventos) a las que aplica.

| kind        | Qué simula                                                                |
|-------------|---------------------------------------------------------------------------|
| `502`/`504` | Respuesta perdida: la escritura SÍ se ejecuta, pero la ciudad recibe 502/504. |
| `503`       | `WALLET_BUSY`: nada se aceptó (`Retry-After`).                             |
| `429`       | `RATE_LIMITED`: nada se aceptó (`Retry-After`).                            |
| `latency`   | La respuesta tarda `seconds` (útil para timeouts del cliente).             |
| `drop`      | El evento no se publica nunca: la ciudad debe reconciliar con `GET /swaps`. |
| `duplicate` | El evento se publica dos veces, con el mismo `msgId`.                      |
| `delay`     | El evento se publica `seconds` después.                                    |
"""

from __future__ import annotations

from dataclasses import dataclass, field

HTTP_KINDS = {"502", "504", "503", "429", "latency"}
EVENT_KINDS = {"drop", "duplicate", "delay"}
OPERATIONS = {"propose", "accept", "cancel", "read"}


@dataclass
class Fault:
    kind: str
    times: int = 1
    operations: set[str] = field(default_factory=lambda: set(OPERATIONS))
    seconds: float = 0.0
    retry_after: int = 1

    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "times": self.times,
            "operations": sorted(self.operations),
            "seconds": self.seconds,
            "retryAfter": self.retry_after,
        }


def parse_fault(body: dict) -> Fault:
    kind = str(body.get("kind", ""))
    if kind not in HTTP_KINDS | EVENT_KINDS:
        raise ValueError(f"kind debe ser uno de {sorted(HTTP_KINDS | EVENT_KINDS)}")
    times = int(body.get("times", 1))
    if times < 1:
        raise ValueError("times debe ser al menos 1")
    operations = set(body.get("operations") or OPERATIONS)
    if not operations <= OPERATIONS:
        raise ValueError(f"operations debe estar en {sorted(OPERATIONS)}")
    return Fault(
        kind, times, operations, float(body.get("seconds", 0)), int(body.get("retryAfter", 1))
    )


class Faults:
    def __init__(self):
        self.active: list[Fault] = []

    def add(self, fault: Fault) -> None:
        self.active.append(fault)

    def clear(self) -> None:
        self.active.clear()

    def _take(self, kinds: set[str], operation: str | None = None) -> Fault | None:
        for fault in self.active:
            if fault.kind in kinds and (operation is None or operation in fault.operations):
                fault.times -= 1
                if fault.times == 0:
                    self.active.remove(fault)
                return fault
        return None

    def take_http(self, operation: str) -> Fault | None:
        return self._take(HTTP_KINDS, operation)

    def take_event(self) -> Fault | None:
        return self._take(EVENT_KINDS)
