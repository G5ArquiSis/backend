"""Reglas de la wallet falsa: saldos, swaps y los eventos que cada operación produce.

Todo es síncrono y en memoria. La capa HTTP (`app.py`) decide cuándo se ejecuta cada
operación (las escrituras se procesan después de responder 202, como la wallet real) y
qué hacer con los eventos que devuelven estas funciones.

Los montos en `CR` se guardan en centavos enteros y los de `KWH` en kWh enteros, como
recomienda `common/cr.schema.json`; se convierten a `number` JSON sólo al salir.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

ASSETS = ("CR", "KWH")
MAX_EXPIRY = timedelta(seconds=60)
# Tolerancia para el tope de 60 s: el reloj del cliente y el de la wallet no coinciden
# exactamente, y la petición tarda en llegar.
EXPIRY_TOLERANCE = timedelta(seconds=2)
MAX_MEMO_BYTES = 128

# (reason, code) de cada rechazo, según reason/refusals.schema.json.
REFUSALS = {
    "MARKET_CLOSED": 410,
    "INSUFFICIENT_ASSET": 402,
    "SWAP_NOT_PROPOSED": 409,
    "SWAP_EXPIRED": 410,
    "NOT_PROPOSER": 403,
    "SELF_SWAP": 422,
    "SAME_ASSET": 422,
}


class RequestError(Exception):
    """Error HTTP síncrono de la wallet: no se ejecutó nada."""

    def __init__(self, status: int, reason: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {
            "reason": self.reason,
            "code": self.status,
            "data": {"message": self.message, **self.extra},
        }


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cr_out(cents: int) -> int | float:
    """Centavos a `number` JSON: entero si no hay decimales, si no con dos."""
    return cents // 100 if cents % 100 == 0 else round(cents / 100, 2)


def amount_out(asset: str, units: int) -> int | float:
    return cr_out(units) if asset == "CR" else units


@dataclass(frozen=True)
class Leg:
    """Una pata de un swap; `units` es centavos en CR y kWh en KWH."""

    asset: str
    units: int

    def out(self) -> dict:
        return {"asset": self.asset, "amount": amount_out(self.asset, self.units)}


@dataclass
class Entry:
    entry_id: str
    cycle_id: str | None
    asset: str
    kind: str
    units: int
    counterparty: str | None
    reference: str
    created_at: datetime

    def out(self) -> dict:
        return {
            "entryId": self.entry_id,
            "cycleId": self.cycle_id,
            "asset": self.asset,
            "kind": self.kind,
            "amount": amount_out(self.asset, self.units),
            "counterparty": self.counterparty,
            "reference": self.reference,
            "createdAt": iso(self.created_at),
        }


@dataclass
class Account:
    city: str
    cr_available: int
    kwh_available: int
    credit_line: int
    cr_held: int = 0
    kwh_held: int = 0
    entries: list[Entry] = field(default_factory=list)

    def available(self, asset: str) -> int:
        return self.cr_available if asset == "CR" else self.kwh_available

    def spendable(self, asset: str) -> int:
        """Lo que se puede retener o entregar: en CR incluye la línea de crédito."""
        return self.cr_available + self.credit_line if asset == "CR" else self.kwh_available

    def move(self, asset: str, available: int = 0, held: int = 0) -> None:
        if asset == "CR":
            self.cr_available += available
            self.cr_held += held
        else:
            self.kwh_available += available
            self.kwh_held += held

    def balances_after(self) -> dict:
        return {
            "CR": {"available": cr_out(self.cr_available), "held": cr_out(self.cr_held)},
            "KWH": {"available": self.kwh_available, "held": self.kwh_held},
        }


@dataclass
class Event:
    """Un evento de la wallet. `data` no incluye `balancesAfter`: se arma para cada
    destinatario (y para cada lector de `GET /swaps/{id}`) con sus propios saldos."""

    type: str
    data: dict
    with_balances: bool = True
    reason: str | None = None
    # Sólo en INSUFFICIENT_ASSET: `available` va únicamente en la copia de la ciudad que
    # intentó la operación, nunca en la del proponente.
    available_for: str | None = None
    available: int | float | None = None


@dataclass
class Outgoing:
    """Un evento con destino: la capa HTTP lo publica en `city.{city}`."""

    city: str
    message: dict


@dataclass
class Swap:
    swap_id: str
    proposer: str
    give: Leg
    take: Leg
    memo: str
    expires_at: datetime
    propose_idpk: str
    created_at: datetime
    status: str = "accepted"
    token: str | None = None
    last_event: Event | None = None


class Wallet:
    def __init__(
        self,
        keys: dict[str, str],
        initial_cr: int,
        initial_kwh: int,
        credit_line: int,
        signer=None,
        cycle_id: str | None = "cycle-local-1",
    ):
        # keys: API key -> código de ciudad.
        self.keys = dict(keys)
        self.initial = (initial_cr, initial_kwh, credit_line)
        self.signer = signer
        self.cycle_id = cycle_id
        self.kwh_enabled = True
        self.reset()

    def reset(self) -> None:
        cr, kwh, line = self.initial
        self.accounts = {
            city: Account(city, cr_available=cr, kwh_available=kwh, credit_line=line)
            for city in self.keys.values()
        }
        self.swaps: dict[str, Swap] = {}
        # (ciudad, Idempotency-Key) -> (huella de la petición, swapId)
        self.idempotency: dict[tuple[str, str], tuple[tuple, str]] = {}
        self.kwh_enabled = True

    # --- Autenticación e idempotencia -------------------------------------------------

    def city_for(self, api_key: str | None) -> str:
        city = self.keys.get(api_key or "")
        if city is None:
            raise RequestError(401, "UNAUTHORIZED", "X-Api-Key missing or invalid")
        return city

    def replay(self, city: str, key: str, fingerprint: tuple) -> dict | None:
        """Estado actual del swap si la llave ya se usó con la misma petición; None si es nueva."""
        known = self.idempotency.get((city, key))
        if known is None:
            return None
        if known[0] != fingerprint:
            raise RequestError(
                422, "IDEMPOTENCY_MISMATCH", "Idempotency-Key already used with a different payload"
            )
        return self.swap_state(known[1], city)

    # --- Escrituras: parte síncrona (antes del 202) -----------------------------------

    def register_propose(self, city: str, key: str, body: dict, now: datetime) -> Swap:
        give, take, expires_at, memo = parse_propose(body, now)
        swap = Swap(str(uuid.uuid4()), city, give, take, memo, expires_at, key, now)
        self.swaps[swap.swap_id] = swap
        self.idempotency[(city, key)] = (("propose", canonical(body)), swap.swap_id)
        return swap

    def register_action(self, city: str, key: str, operation: str, swap_id: str) -> Swap:
        swap = self.swaps.get(swap_id)
        if swap is None:
            raise RequestError(404, "SWAP_UNKNOWN", "swap not found", swapId=swap_id)
        self.idempotency[(city, key)] = ((operation, swap_id), swap_id)
        return swap

    # --- Escrituras: procesamiento (después del 202) ----------------------------------

    def process_propose(self, swap: Swap, now: datetime) -> list[Outgoing]:
        account = self.accounts[swap.proposer]
        if swap.give.asset == swap.take.asset:
            return self._reject(
                swap,
                "propose",
                swap.propose_idpk,
                swap.proposer,
                "SAME_ASSET",
                "give and take must be different assets",
            )
        if "KWH" in (swap.give.asset, swap.take.asset) and not self.kwh_enabled:
            return self._reject(
                swap,
                "propose",
                swap.propose_idpk,
                swap.proposer,
                "MARKET_CLOSED",
                "KWH is disabled outside market hours",
            )
        if account.spendable(swap.give.asset) < swap.give.units:
            return self._insufficient(
                swap, "propose", swap.propose_idpk, swap.proposer, swap.give, notify_proposer=False
            )

        account.move(swap.give.asset, available=-swap.give.units, held=swap.give.units)
        swap.status = "proposed"
        if self.signer is not None:
            swap.token = self.signer.sign_swap(swap)
        event = Event(
            "swap-proposed",
            {
                "swapId": swap.swap_id,
                "idpk": swap.propose_idpk,
                "proposer": swap.proposer,
                "give": swap.give.out(),
                "take": swap.take.out(),
                "memo": swap.memo,
                "expiresAt": iso(swap.expires_at),
                "token": swap.token or "",
            },
        )
        swap.last_event = event
        return [self._outgoing(swap.proposer, event)]

    def process_accept(self, swap: Swap, acceptor: str, key: str, now: datetime) -> list[Outgoing]:
        if swap.status == "proposed" and now >= swap.expires_at:
            expired = self._expire(swap)
            return expired + self._reject_only(
                swap, "accept", key, acceptor, "SWAP_EXPIRED", "swap expired"
            )
        if swap.status == "expired":
            return self._reject_only(swap, "accept", key, acceptor, "SWAP_EXPIRED", "swap expired")
        if swap.status != "proposed":
            return self._reject_only(
                swap, "accept", key, acceptor, "SWAP_NOT_PROPOSED", f"swap is {swap.status}"
            )
        if acceptor == swap.proposer:
            return self._reject_only(
                swap,
                "accept",
                key,
                acceptor,
                "SELF_SWAP",
                "the proposer cannot accept its own swap",
            )
        if "KWH" in (swap.give.asset, swap.take.asset) and not self.kwh_enabled:
            return self._reject_only(
                swap,
                "accept",
                key,
                acceptor,
                "MARKET_CLOSED",
                "KWH is disabled outside market hours",
            )

        buyer = self.accounts[acceptor]
        seller = self.accounts[swap.proposer]
        if buyer.spendable(swap.take.asset) < swap.take.units:
            # Nada se mueve en ninguna pata y la retención del proponente se libera.
            seller.move(swap.give.asset, available=swap.give.units, held=-swap.give.units)
            return self._insufficient(
                swap, "accept", key, acceptor, swap.take, notify_proposer=True
            )

        # Las dos patas en una sola "transacción": nada de esto puede fallar a medias.
        seller.move(swap.give.asset, held=-swap.give.units)
        buyer.move(swap.give.asset, available=swap.give.units)
        buyer.move(swap.take.asset, available=-swap.take.units)
        seller.move(swap.take.asset, available=swap.take.units)
        for account, other, sign_give, sign_take in (
            (seller, buyer, -1, +1),
            (buyer, seller, +1, -1),
        ):
            for leg, sign in ((swap.give, sign_give), (swap.take, sign_take)):
                account.entries.append(
                    Entry(
                        str(uuid.uuid4()),
                        self.cycle_id,
                        leg.asset,
                        "swap",
                        sign * leg.units,
                        other.city,
                        swap.swap_id,
                        now,
                    )
                )
        swap.status = "settled"
        event = Event(
            "swap-settled",
            {
                "swapId": swap.swap_id,
                "idpk": key,
                "proposer": swap.proposer,
                "acceptor": acceptor,
                "give": swap.give.out(),
                "take": swap.take.out(),
                "memo": swap.memo,
                "settledAt": iso(now),
            },
        )
        swap.last_event = event
        return [self._outgoing(swap.proposer, event), self._outgoing(acceptor, event)]

    def process_cancel(self, swap: Swap, city: str, key: str, now: datetime) -> list[Outgoing]:
        if city != swap.proposer:
            return self._reject_only(
                swap, "cancel", key, city, "NOT_PROPOSER", "only the proposer can cancel"
            )
        if swap.status == "proposed" and now >= swap.expires_at:
            return self._expire(swap) + self._reject_only(
                swap, "cancel", key, city, "SWAP_NOT_PROPOSED", "swap is expired"
            )
        if swap.status != "proposed":
            return self._reject_only(
                swap, "cancel", key, city, "SWAP_NOT_PROPOSED", f"swap is {swap.status}"
            )
        return self._cancel(swap, key)

    # --- Procesos de la wallet --------------------------------------------------------

    def expire_due(self, now: datetime) -> list[Outgoing]:
        out: list[Outgoing] = []
        for swap in list(self.swaps.values()):
            if swap.status == "proposed" and now >= swap.expires_at:
                out += self._expire(swap)
        return out

    def set_kwh_enabled(self, enabled: bool) -> list[Outgoing]:
        """Abre o cierra el mercado. Al cerrarlo, la central cancela todo swap con KWH
        pendiente, con el idpk de su propia operación (que ninguna ciudad reconoce)."""
        self.kwh_enabled = enabled
        if enabled:
            return []
        central_idpk = str(uuid.uuid4())
        out: list[Outgoing] = []
        for swap in list(self.swaps.values()):
            if swap.status == "proposed" and "KWH" in (swap.give.asset, swap.take.asset):
                out += self._cancel(swap, central_idpk)
        return out

    def set_account(
        self, city: str, cr: float | None, kwh: int | None, credit_line: float | None, now: datetime
    ) -> Account:
        account = self.accounts.get(city)
        if account is None:
            account = self.accounts[city] = Account(city, 0, 0, self.initial[2])
        if cr is not None:
            delta = round(cr * 100) - account.cr_available
            account.cr_available += delta
            if delta:
                account.entries.append(
                    Entry(
                        str(uuid.uuid4()),
                        self.cycle_id,
                        "CR",
                        "adjustment",
                        delta,
                        None,
                        "fake-wallet",
                        now,
                    )
                )
        if kwh is not None:
            delta = int(kwh) - account.kwh_available
            account.kwh_available += delta
            if delta:
                account.entries.append(
                    Entry(
                        str(uuid.uuid4()),
                        self.cycle_id,
                        "KWH",
                        "adjustment",
                        delta,
                        None,
                        "fake-wallet",
                        now,
                    )
                )
        if credit_line is not None:
            account.credit_line = round(credit_line * 100)
        return account

    # --- Lecturas -----------------------------------------------------------------------

    def swap_state(self, swap_id: str, reader: str) -> dict:
        swap = self.swaps.get(swap_id)
        if swap is None:
            raise RequestError(404, "SWAP_UNKNOWN", "swap not found", swapId=swap_id)
        state = {"swapId": swap.swap_id, "status": swap.status}
        if swap.last_event is not None:
            message = self.render(swap.last_event, reader)
            state["lastEvent"] = {
                k: message[k] for k in ("type", "reason", "code", "data") if k in message
            }
        return state

    def balance(self, city: str, now: datetime) -> dict:
        account = self.accounts[city]
        return {
            "cityId": city,
            "assets": {
                "CR": {
                    "available": cr_out(account.cr_available),
                    "held": cr_out(account.cr_held),
                    "creditLine": cr_out(account.credit_line),
                },
                "KWH": {"available": account.kwh_available, "held": account.kwh_held},
            },
            "asOf": iso(now),
            "lastSettledCycle": None,
            "balanceAtLastClose": None,
        }

    def statement(
        self, city: str, cycle_id: str | None, asset: str | None, offset: int, limit: int
    ) -> tuple[list[dict], int | None]:
        entries = [
            entry
            for entry in reversed(self.accounts[city].entries)
            if (cycle_id is None or entry.cycle_id == cycle_id)
            and (asset is None or entry.asset == asset)
        ]
        page = entries[offset : offset + limit]
        following = offset + limit if offset + limit < len(entries) else None
        return [entry.out() for entry in page], following

    # --- Mensajes -----------------------------------------------------------------------

    def render(self, event: Event, city: str) -> dict:
        """El mensaje tal como lo recibe (o lo lee) `city`, con el envelope de la E1."""
        data = dict(event.data)
        if event.available_for is not None and event.available_for == city:
            data["available"] = event.available
        if event.with_balances:
            data["balancesAfter"] = self.accounts[city].balances_after()
        message = {
            "idpk": str(uuid.uuid4()),
            "msgId": str(uuid.uuid4()),
            "type": event.type,
            "timestamp": iso(now_utc()),
            "sender": "central",
        }
        if event.reason is not None:
            message["reason"] = event.reason
            message["code"] = REFUSALS[event.reason]
        message["data"] = data
        return message

    def _outgoing(self, city: str, event: Event) -> Outgoing:
        return Outgoing(city, self.render(event, city))

    def _cancel(self, swap: Swap, idpk: str) -> list[Outgoing]:
        self.accounts[swap.proposer].move(
            swap.give.asset, available=swap.give.units, held=-swap.give.units
        )
        swap.status = "cancelled"
        event = Event("swap-cancelled", {"swapId": swap.swap_id, "idpk": idpk})
        swap.last_event = event
        return [self._outgoing(swap.proposer, event)]

    def _expire(self, swap: Swap) -> list[Outgoing]:
        self.accounts[swap.proposer].move(
            swap.give.asset, available=swap.give.units, held=-swap.give.units
        )
        swap.status = "expired"
        event = Event("swap-expired", {"swapId": swap.swap_id})
        swap.last_event = event
        return [self._outgoing(swap.proposer, event)]

    def _rejection(self, swap: Swap, operation: str, key: str, reason: str, message: str) -> Event:
        return Event(
            "swap-rejected",
            {
                "swapId": swap.swap_id,
                "idpk": key,
                "operation": operation,
                "message": message,
            },
            with_balances=False,
            reason=reason,
        )

    def _reject(
        self, swap: Swap, operation: str, key: str, city: str, reason: str, message: str
    ) -> list[Outgoing]:
        """Rechazo que resuelve el swap (al proponer): pasa a `rejected`."""
        event = self._rejection(swap, operation, key, reason, message)
        swap.status = "rejected"
        swap.last_event = event
        return [self._outgoing(city, event)]

    def _reject_only(
        self, swap: Swap, operation: str, key: str, city: str, reason: str, message: str
    ) -> list[Outgoing]:
        """Rechazo de una operación que no cambia el swap (por ejemplo, aceptar uno ya
        liquidado): sólo lo recibe quien intentó la operación."""
        return [self._outgoing(city, self._rejection(swap, operation, key, reason, message))]

    def _insufficient(
        self, swap: Swap, operation: str, key: str, city: str, leg: Leg, notify_proposer: bool
    ) -> list[Outgoing]:
        available = amount_out(leg.asset, self.accounts[city].available(leg.asset))
        required = amount_out(leg.asset, leg.units)
        event = self._rejection(
            swap,
            operation,
            key,
            "INSUFFICIENT_ASSET",
            f"{leg.asset} available {available}, required {required}",
        )
        event.data.update({"asset": leg.asset, "required": required})
        event.available_for = city
        event.available = available
        swap.status = "rejected"
        swap.last_event = event
        out = [self._outgoing(city, event)]
        if notify_proposer:
            out.append(self._outgoing(swap.proposer, event))
        return out


# --- Validación del cuerpo de POST /swaps -----------------------------------------------


def canonical(value) -> tuple:
    """Huella comparable de un JSON: dos cuerpos iguales dan la misma huella."""
    if isinstance(value, dict):
        return tuple(sorted((k, canonical(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(canonical(v) for v in value)
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return (type(value).__name__, value)
    return ("number", float(value))


def malformed(message: str) -> RequestError:
    return RequestError(422, "MALFORMED_REQUEST", message)


def parse_leg(name: str, value) -> Leg:
    if not isinstance(value, dict) or set(value) != {"asset", "amount"}:
        raise malformed(f"{name} must be an object with exactly asset and amount")
    asset, amount = value["asset"], value["amount"]
    if asset not in ASSETS:
        raise malformed(f"{name}.asset must be CR or KWH")
    if isinstance(amount, bool) or not isinstance(amount, int | float) or amount <= 0:
        raise malformed(f"{name}.amount must be a positive number")
    if asset == "KWH":
        if not float(amount).is_integer():
            raise malformed(f"{name}.amount must be an integer for KWH")
        return Leg("KWH", int(amount))
    cents = round(amount * 100)
    if abs(amount * 100 - cents) > 1e-6:
        raise malformed(f"{name}.amount must have at most two decimals for CR")
    return Leg("CR", cents)


def parse_propose(body, now: datetime) -> tuple[Leg, Leg, datetime, str]:
    if not isinstance(body, dict):
        raise malformed("body must be a JSON object")
    required = {"give", "take", "expiresAt", "memo"}
    missing = required - set(body)
    if missing:
        raise malformed(f"{sorted(missing)[0]} is required")
    extra = set(body) - required
    if extra:
        raise malformed(f"unexpected field {sorted(extra)[0]}")
    give = parse_leg("give", body["give"])
    take = parse_leg("take", body["take"])
    memo = body["memo"]
    if not isinstance(memo, str) or len(memo.encode()) > MAX_MEMO_BYTES:
        raise malformed(f"memo must be a string of at most {MAX_MEMO_BYTES} bytes")
    try:
        expires_at = datetime.fromisoformat(str(body["expiresAt"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise malformed("expiresAt must be an ISO 8601 instant") from error
    if expires_at.tzinfo is None:
        raise malformed("expiresAt must include a timezone")
    if expires_at <= now:
        raise malformed("expiresAt must be in the future")
    if expires_at > now + MAX_EXPIRY + EXPIRY_TOLERANCE:
        raise malformed("expiresAt must be at most 60 seconds from now")
    return give, take, expires_at, memo
