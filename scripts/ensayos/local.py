"""Piezas comunes de los ensayos de anomalías (E7): broker, wallet falsa, master y docker.

Sólo trabajan contra el stack local con el perfil `fake` (`docker compose --profile fake up`):
el broker y la wallet son los locales y master es `127.0.0.1`. `exigir_local()` corta el ensayo
si alguna URL apunta a otro lado: estos scripts inyectan mensajes y fallas, y en producción
alterarían el ledger que se le reporta a la central.

Identidades del broker local (`scripts/fake_wallet/rabbitmq/definitions.json`, no son secretos):

- `central` publica como la central (`user_id` "central"): distance-table, eventos de la wallet.
- `city.HGW` es la contraparte simulada: oferta en `market`, bids y respuestas por `energy.x`.
  Lee `city.HGW.q` (lo que TAL le manda y sus eventos de la wallet) y `market.HGW` (el feed).
- `admin` sólo se usa para la API de administración (cerrar conexiones en la anomalía 5).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import pika

BACKEND = Path(__file__).resolve().parents[2]

MASTER = os.environ.get("ENSAYO_MASTER_URL", "http://127.0.0.1:8001")
WALLET = os.environ.get("ENSAYO_WALLET_URL", "http://127.0.0.1:8090")
BROKER_HOST = os.environ.get("ENSAYO_BROKER_HOST", "127.0.0.1")
BROKER_ADMIN = os.environ.get("ENSAYO_BROKER_ADMIN_URL", "http://127.0.0.1:15672")
VHOST = "energy"

# Credenciales del broker local; las mismas de definitions.json.
CREDENCIALES = {
    "central": "local-central",
    "city.TAL": "local-tal",
    "city.HGW": "local-hgw",
    "admin": "admin",
}
WALLET_KEYS = {"TAL": "fake-key-tal", "HGW": "fake-key-hgw", "COR": "fake-key-cor"}

_LOCALES = {"127.0.0.1", "localhost", "::1"}


def exigir_local() -> None:
    for nombre, url in (("master", MASTER), ("wallet", WALLET), ("broker", BROKER_ADMIN)):
        host = urlparse(url).hostname
        if host not in _LOCALES:
            sys.exit(f"{nombre} apunta a {host}: los ensayos sólo corren contra el stack local")
    if BROKER_HOST not in _LOCALES:
        sys.exit(f"el broker es {BROKER_HOST}: los ensayos sólo corren contra el stack local")


# --- Salida ----------------------------------------------------------------------------------


@dataclass
class Resultado:
    """Lo que se esperaba ver y lo que se vio. El script sale con 1 si algo no se cumplió."""

    anomalia: str
    fallas: list[str] = field(default_factory=list)

    def paso(self, titulo: str) -> None:
        print(f"\n== [{self.anomalia}] {titulo}")

    def info(self, texto: str) -> None:
        print(f"   {texto}")

    def chequeo(self, condicion: bool, texto: str, detalle: str = "") -> bool:
        print(f"   {'✓' if condicion else '✗'} {texto}{f' ({detalle})' if detalle else ''}")
        if not condicion:
            self.fallas.append(texto)
        return condicion

    def terminar(self) -> None:
        print()
        if self.fallas:
            print(f"[{self.anomalia}] {len(self.fallas)} comprobación(es) no se cumplieron:")
            for falla in self.fallas:
                print(f"   - {falla}")
            sys.exit(1)
        print(f"[{self.anomalia}] todo lo esperado se observó.")


# --- HTTP ------------------------------------------------------------------------------------


def http(method: str, url: str, body=None, headers: dict | None = None, timeout: float = 15):
    """(status, json) con la librería estándar. Status 0 si no hubo respuesta."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return 0, str(error)


def master(path: str):
    return http("GET", f"{MASTER}{path}")


def message_log(limit: int = 100) -> list[dict]:
    status, body = master(f"/message-log?limit={limit}")
    if status != 200:
        return []
    return body.get("items", body) if isinstance(body, dict) else body


def wallet_admin(method: str, path: str, body=None):
    return http(method, f"{WALLET}/_fake{path}", body)


def wallet_v1(method: str, path: str, city: str, body=None):
    headers = {"X-Api-Key": WALLET_KEYS[city]}
    if method == "POST":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return http(method, f"{WALLET}/v1{path}", body, headers)


def wallet_events(city: str, message_type: str | None = None) -> list[dict]:
    query = f"?city={city}&limit=500" + (f"&type={message_type}" if message_type else "")
    status, body = wallet_admin("GET", f"/events{query}")
    return [e["message"] for e in body] if status == 200 else []


def wallet_balance(city: str) -> dict:
    status, body = wallet_admin("GET", "/state")
    return body["balances"].get(city, {}) if status == 200 else {}


def esperar(condicion: Callable[[], object], segundos: float, cada: float = 0.5):
    """Devuelve el primer valor verdadero de `condicion`, o None si se acaba el plazo."""
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        valor = condicion()
        if valor:
            return valor
        time.sleep(cada)
    return None


def stack_arriba(resultado: Resultado) -> None:
    resultado.paso("Stack local")
    ok_master = master("/health")[0] == 200
    ok_wallet = http("GET", f"{WALLET}/health")[0] == 200
    resultado.chequeo(ok_master, "master responde /health", MASTER)
    resultado.chequeo(ok_wallet, "la wallet falsa responde /health", WALLET)
    if not (ok_master and ok_wallet):
        sys.exit("levantar el stack: docker compose --profile fake up --build")


# --- Mensajes --------------------------------------------------------------------------------


def ahora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def en(segundos: float) -> str:
    instante = datetime.now(UTC) + timedelta(seconds=segundos)
    return instante.isoformat(timespec="seconds").replace("+00:00", "Z")


def mensaje(
    tipo: str,
    data: dict,
    *,
    ciudad: str | None = None,
    ciclo: str | None = None,
    idpk: str | None = None,
) -> dict:
    """Envelope v3: de una ciudad (`cityId`) o de la central (`sender`)."""
    cuerpo = {
        "idpk": idpk or str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": tipo,
        "timestamp": ahora(),
    }
    if ciudad:
        cuerpo["cityId"] = ciudad
    else:
        cuerpo["sender"] = "central"
    if ciclo:
        cuerpo["cycleId"] = ciclo
    cuerpo["data"] = data
    return cuerpo


def status_statement(
    ciclo: str,
    *,
    mercado: float,
    cierre: float,
    generacion: float = 26000,
    consumo: float = 13000,
    min_bid: int = 10,
) -> dict:
    """Status-statement v3 con los tiempos comprimidos: el mercado abre ahora y dura `mercado`
    segundos, y el periodo de cierre (`reportOpensAt` → `validUntil`) dura `cierre`. Mismas
    proporciones que el ciclo real (8:30/9:00/10:00 de mercado)."""
    sellable = int(max(0, generacion - consumo))
    return mensaje(
        "status-statement",
        {
            "energy": {
                "generationCapacity": generacion,
                "consumption": consumo,
                "generationCost": 43.2,
                "sellable": sellable,
            },
            "market": {
                "buybackPrice": 45.36,
                "imbalancePrice": 735,
                "minBid": min_bid,
                "opensAt": en(0),
                "lastOfferAt": en(mercado * 0.85),
                "lastBidAt": en(mercado * 0.9),
                "closesAt": en(mercado),
                "reportOpensAt": en(mercado),
                "limits": {
                    "marketOpsPerMinute": 60,
                    "walletWritesPerMinute": 120,
                    "walletReadsPerMinute": 300,
                },
            },
            "validUntil": en(mercado + cierre),
        },
        ciclo=ciclo,
    )


def ciclo_nuevo(prefijo: str) -> str:
    """Un ciclo propio del ensayo, también como ciclo de la wallet falsa."""
    ciclo = f"cycle-{prefijo}-{int(time.time())}"
    wallet_admin("POST", "/cycle", {"cycleId": ciclo})
    return ciclo


def esperar_reporte(central: Broker, ciclo: str, segundos: float) -> list[dict]:
    """Todos los negotiation-report de `ciclo` que lleguen a `central.q` en ese plazo."""
    reportes = []
    limite = time.monotonic() + segundos
    while (restante := limite - time.monotonic()) > 0:
        reporte = central.leer(
            "central.q",
            restante,
            lambda m: m.get("type") == "negotiation-report" and m.get("cycleId") == ciclo,
        )
        if reporte is None:
            break
        reportes.append(reporte)
    return reportes


def ciclo_actual() -> str:
    """El ciclo que la wallet falsa usa para sus eventos, o uno de ensayo si no tiene."""
    status, body = wallet_admin("GET", "/state")
    if status == 200 and body.get("cycleId"):
        return body["cycleId"]
    ciclo = f"cycle-ensayo-{int(time.time()) // 1200}"
    wallet_admin("POST", "/cycle", {"cycleId": ciclo})
    return ciclo


# --- Broker ----------------------------------------------------------------------------------


class Broker:
    """Una conexión AMQP con la identidad `usuario` (y `user_id` igual, como exige el curso)."""

    def __init__(self, usuario: str):
        self.usuario = usuario
        credenciales = pika.PlainCredentials(usuario, CREDENCIALES[usuario])
        self.conexion = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=BROKER_HOST,
                virtual_host=VHOST,
                credentials=credenciales,
                client_properties={"connection_name": f"ensayo-{usuario}"},
            )
        )
        self.canal = self.conexion.channel()
        self._pendientes: dict[str, list[dict]] = {}

    def publicar(
        self, exchange: str, routing_key: str, cuerpo: dict | bytes, user_id: str | None = ""
    ) -> None:
        """`user_id=""` usa la identidad de la conexión; `None` publica sin `user_id`."""
        if user_id == "":
            user_id = self.usuario
        payload = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        self.canal.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=payload,
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2, user_id=user_id
            ),
        )

    def a_ciudad(self, ciudad: str, cuerpo: dict | bytes, user_id: str | None = "") -> None:
        self.publicar("energy.x", f"city.{ciudad}", cuerpo, user_id)

    def leer(
        self,
        cola: str,
        segundos: float,
        filtro: Callable[[dict], bool] = lambda m: True,
    ) -> dict | None:
        """Primer mensaje de `cola` que cumple `filtro`. Los que no lo cumplen quedan guardados
        para una lectura siguiente, así el orden de llegada no importa."""
        pendientes = self._pendientes.setdefault(cola, [])
        limite = time.monotonic() + segundos
        while True:
            for i, recibido in enumerate(pendientes):
                if filtro(recibido):
                    return pendientes.pop(i)
            if time.monotonic() >= limite:
                return None
            metodo, _props, cuerpo = self.canal.basic_get(cola, auto_ack=True)
            if metodo is None:
                self.conexion.sleep(0.3)
                continue
            try:
                recibido = json.loads(cuerpo)
            except ValueError:
                continue
            if isinstance(recibido, dict):
                pendientes.append(recibido)

    def vaciar(self, cola: str) -> int:
        self._pendientes.pop(cola, None)
        return self.canal.queue_purge(cola).method.message_count

    def cerrar(self) -> None:
        if self.conexion.is_open:
            self.conexion.close()


def conexiones_de(usuario: str) -> list[str]:
    """Nombres de las conexiones abiertas de `usuario`, vía la API de administración."""
    status, body = http("GET", f"{BROKER_ADMIN}/api/connections", headers=_admin_auth())
    if status != 200:
        return []
    return [c["name"] for c in body if c.get("user") == usuario]


def cerrar_conexion(nombre: str) -> int:
    headers = {**_admin_auth(), "X-Reason": "ensayo de corte del broker"}
    return http(
        "DELETE", f"{BROKER_ADMIN}/api/connections/{quote(nombre, safe='')}", headers=headers
    )[0]


def _admin_auth() -> dict:
    token = base64.b64encode(f"admin:{CREDENCIALES['admin']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- Docker ----------------------------------------------------------------------------------


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "--profile", "fake", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=check,
    )


def estado_contenedor(servicio: str) -> str:
    """running/healthy/unhealthy/exited..., según `docker compose ps`."""
    salida = compose("ps", "--all", "--format", "json", servicio, check=False).stdout.strip()
    if not salida:
        return "ausente"
    fila = json.loads(salida.splitlines()[0])
    return fila.get("Health") or fila.get("State", "?")


def logs_desde(servicio: str, desde: str) -> str:
    return compose("logs", "--no-color", "--since", desde, servicio, check=False).stdout
