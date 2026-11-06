"""Wallet falsa de EnergyShark: la API v3 del curso (`wallet.openapi.yaml`, v3.0.2) para
desarrollo local y pruebas. NO es la wallet real ni guarda nada: todo vive en memoria y se
pierde al reiniciar.

- `/v1/...` y `/.well-known/jwks.json`: lo mismo que la wallet real (mismas rutas, mismos
  status y cuerpos, escrituras asíncronas con 202 y eventos a `city.{CODE}`).
- `/_fake/...`: controles que la wallet real no tiene (saldos, fallas, cierre del mercado,
  registro de eventos). Ver README.md.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import uuid
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .core import ASSETS, RequestError, Wallet, canonical, iso, malformed, now_utc
from .faults import Faults, parse_fault
from .tokens import Signer

logger = logging.getLogger("fake_wallet")

DEFAULT_KEYS = "TAL=fake-key-tal,HGW=fake-key-hgw,COR=fake-key-cor"
MAX_PAGE = 200


@dataclass
class Config:
    # Código de ciudad -> API key. Las demás ciudades sirven para hacer de contraparte.
    keys: dict[str, str] = field(default_factory=lambda: parse_keys(DEFAULT_KEYS))
    initial_cr: float = 600_000
    initial_kwh: int = 20_000
    credit_line: float = 1_200_000
    # Cuánto tarda en procesarse una escritura después del 202.
    processing_seconds: float = 0.3
    expiry_poll_seconds: float = 0.5
    amqp_url: str = ""
    signing_key: str = ""

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ.get
        return cls(
            keys=parse_keys(env("FAKE_WALLET_KEYS", DEFAULT_KEYS)),
            initial_cr=float(env("FAKE_WALLET_INITIAL_CR", 600_000)),
            initial_kwh=int(env("FAKE_WALLET_INITIAL_KWH", 20_000)),
            credit_line=float(env("FAKE_WALLET_CREDIT_LINE", 1_200_000)),
            processing_seconds=float(env("FAKE_WALLET_PROCESSING_SECONDS", 0.3)),
            expiry_poll_seconds=float(env("FAKE_WALLET_EXPIRY_POLL_SECONDS", 0.5)),
            amqp_url=env("FAKE_WALLET_AMQP_URL", ""),
            signing_key=env("FAKE_WALLET_SIGNING_KEY", ""),
        )


def parse_keys(raw: str) -> dict[str, str]:
    keys = {}
    for pair in filter(None, (part.strip() for part in raw.split(","))):
        city, _, key = pair.partition("=")
        if not city or not key:
            raise ValueError(f"FAKE_WALLET_KEYS mal formado: {pair!r} (se espera CIUDAD=clave)")
        keys[city.strip()] = key.strip()
    return keys


def error(exc: RequestError, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(exc.body(), status_code=exc.status, headers=headers)


def create_app(config: Config | None = None, sink=None) -> FastAPI:
    config = config or Config.from_env()
    if sink is None and config.amqp_url:
        from .sinks import AmqpSink

        sink = AmqpSink(config.amqp_url)

    signer = Signer(pem_path=config.signing_key or None)
    wallet = Wallet(
        keys={key: city for city, key in config.keys.items()},
        initial_cr=round(config.initial_cr * 100),
        initial_kwh=config.initial_kwh,
        credit_line=round(config.credit_line * 100),
        signer=signer,
    )
    faults = Faults()
    events: deque[dict] = deque(maxlen=1000)
    pending: set[asyncio.Task] = set()

    def spawn(coroutine) -> None:
        task = asyncio.create_task(coroutine)
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def deliver(city: str, message: dict, note: str | None) -> None:
        events.append(
            {"city": city, "message": message, "publishedAt": iso(now_utc()), "fault": note}
        )
        if sink is not None:
            await sink.publish(city, message)

    async def dispatch(outgoing) -> None:
        for item in outgoing:
            fault = faults.take_event()
            if fault is None:
                await deliver(item.city, item.message, None)
            elif fault.kind == "drop":
                events.append(
                    {
                        "city": item.city,
                        "message": item.message,
                        "publishedAt": None,
                        "fault": "drop",
                    }
                )
                logger.info("Falla inyectada: se descarta %s a %s", item.message["type"], item.city)
            elif fault.kind == "duplicate":
                await deliver(item.city, item.message, "duplicate")
                await deliver(item.city, item.message, "duplicate")
            else:  # delay

                async def later(item=item, seconds=fault.seconds):
                    await asyncio.sleep(seconds)
                    await deliver(item.city, item.message, "delay")

                spawn(later())

    def process_later(step) -> None:
        async def run():
            await asyncio.sleep(config.processing_seconds)
            await dispatch(step(now_utc()))

        spawn(run())

    async def expiry_loop():
        while True:
            await asyncio.sleep(config.expiry_poll_seconds)
            await dispatch(wallet.expire_due(now_utc()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        expiry = asyncio.create_task(expiry_loop())
        yield
        expiry.cancel()
        with suppress(asyncio.CancelledError):
            await expiry
        if sink is not None and hasattr(sink, "close"):
            sink.close()

    app = FastAPI(title="Wallet falsa EnergyShark", version="3.0.2-fake", lifespan=lifespan)
    app.state.wallet = wallet
    app.state.faults = faults
    app.state.events = events
    app.state.signer = signer

    async def settle() -> None:
        """Espera a que terminen las escrituras y eventos en curso (para tests)."""
        while pending:
            await asyncio.gather(*list(pending))

    app.state.settle = settle

    @app.exception_handler(RequestError)
    async def on_request_error(_request: Request, exc: RequestError):
        return error(exc)

    def idempotency_key(request: Request) -> str:
        raw = request.headers.get("Idempotency-Key")
        if not raw:
            raise malformed("Idempotency-Key is required")
        try:
            return str(uuid.UUID(raw))
        except ValueError as exc:
            raise malformed("Idempotency-Key must be a UUID") from exc

    async def write(request: Request, operation: str, swap_id: str | None = None) -> Response:
        city = wallet.city_for(request.headers.get("X-Api-Key"))
        fault = faults.take_http(operation)
        if fault is not None and fault.kind == "503":
            return error(
                RequestError(
                    503,
                    "WALLET_BUSY",
                    "wallet busy; nothing was accepted, retry with the same Idempotency-Key",
                ),
                fault.retry_after,
            )
        if fault is not None and fault.kind == "429":
            return error(
                RequestError(
                    429,
                    "RATE_LIMITED",
                    "wallet write limit exceeded; retry after the Retry-After header",
                ),
                fault.retry_after,
            )
        if fault is not None and fault.kind == "latency":
            await asyncio.sleep(fault.seconds)

        key = idempotency_key(request)
        if operation == "propose":
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise malformed("body must be valid JSON") from exc
            fingerprint = ("propose", canonical(body))
        else:
            fingerprint = (operation, swap_id)

        replayed = wallet.replay(city, key, fingerprint)
        if replayed is not None:
            response: Response = JSONResponse(replayed, status_code=200)
        else:
            now = now_utc()
            if operation == "propose":
                swap = wallet.register_propose(city, key, body, now)
                process_later(lambda at, swap=swap: wallet.process_propose(swap, at))
            else:
                swap = wallet.register_action(city, key, operation, swap_id)
                step = wallet.process_accept if operation == "accept" else wallet.process_cancel
                process_later(lambda at, swap=swap: step(swap, city, key, at))
            response = JSONResponse({"swapId": swap.swap_id, "status": "accepted"}, status_code=202)

        # La operación ya quedó aceptada: el 502/504 sólo pierde la respuesta.
        if fault is not None and fault.kind in ("502", "504"):
            if fault.kind == "504":
                await asyncio.sleep(fault.seconds)
            text = "Bad Gateway" if fault.kind == "502" else "Gateway Timeout"
            return PlainTextResponse(text, status_code=int(fault.kind))
        return response

    async def read_guard(request: Request) -> tuple[str, Response | None]:
        city = wallet.city_for(request.headers.get("X-Api-Key"))
        fault = faults.take_http("read")
        if fault is None:
            return city, None
        if fault.kind == "latency":
            await asyncio.sleep(fault.seconds)
            return city, None
        if fault.kind == "429":
            return city, error(
                RequestError(429, "RATE_LIMITED", "read limit exceeded"), fault.retry_after
            )
        if fault.kind == "503":
            return city, error(RequestError(503, "WALLET_BUSY", "wallet busy"), fault.retry_after)
        if fault.kind == "504":
            await asyncio.sleep(fault.seconds)
        return city, PlainTextResponse(
            "Bad Gateway" if fault.kind == "502" else "Gateway Timeout", status_code=int(fault.kind)
        )

    v1 = APIRouter(prefix="/v1")

    @v1.post("/swaps")
    async def propose_swap(request: Request):
        return await write(request, "propose")

    @v1.post("/swaps/{swap_id}/accept")
    async def accept_swap(swap_id: str, request: Request):
        return await write(request, "accept", swap_id)

    @v1.post("/swaps/{swap_id}/cancel")
    async def cancel_swap(swap_id: str, request: Request):
        return await write(request, "cancel", swap_id)

    @v1.get("/swaps/{swap_id}")
    async def get_swap(swap_id: str, request: Request):
        city, failed = await read_guard(request)
        return failed or wallet.swap_state(swap_id, city)

    @v1.get("/balance")
    async def get_balance(request: Request):
        city, failed = await read_guard(request)
        return failed or wallet.balance(city, now_utc())

    @v1.get("/statement")
    async def get_statement(request: Request):
        city, failed = await read_guard(request)
        if failed:
            return failed
        query = request.query_params
        asset = query.get("asset")
        if asset is not None and asset not in ASSETS:
            raise malformed("asset must be CR or KWH")
        try:
            limit = int(query.get("limit", 50))
            offset = 0
            if query.get("cursor"):
                offset = json.loads(base64.urlsafe_b64decode(query["cursor"] + "=="))["offset"]
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise malformed("invalid limit or cursor") from exc
        if limit < 1:
            raise malformed("limit must be at least 1")
        entries, following = wallet.statement(
            city, query.get("cycleId"), asset, offset, min(limit, MAX_PAGE)
        )
        cursor = None
        if following is not None:
            raw = json.dumps({"offset": following}).encode()
            cursor = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        return {"entries": entries, "nextCursor": cursor}

    @v1.get("/leaderboard")
    async def get_leaderboard(request: Request):
        city, failed = await read_guard(request)
        return failed or {"cityId": city, "score": 0, "cyclesComputed": 0, "position": 1}

    app.include_router(v1)

    @app.get("/.well-known/jwks.json")
    async def jwks():
        return signer.jwks()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # --- Controles de la wallet falsa (no existen en la real) -------------------------

    admin = APIRouter(prefix="/_fake", tags=["fake"])

    @admin.get("/state")
    async def state():
        return {
            "kwhEnabled": wallet.kwh_enabled,
            "cycleId": wallet.cycle_id,
            "balances": {
                city: wallet.balance(city, now_utc())["assets"] for city in wallet.accounts
            },
            "swaps": [
                {
                    "swapId": s.swap_id,
                    "proposer": s.proposer,
                    "status": s.status,
                    "give": s.give.out(),
                    "take": s.take.out(),
                    "memo": s.memo,
                    "expiresAt": iso(s.expires_at),
                }
                for s in wallet.swaps.values()
            ],
            "faults": [f.describe() for f in faults.active],
        }

    @admin.post("/reset")
    async def reset():
        wallet.reset()
        faults.clear()
        events.clear()
        return {"status": "reset"}

    @admin.post("/accounts/{city}")
    async def set_account(city: str, request: Request):
        body = await request.json()
        wallet.set_account(city, body.get("cr"), body.get("kwh"), body.get("creditLine"), now_utc())
        return wallet.balance(city, now_utc())

    @admin.get("/faults")
    async def list_faults():
        return [f.describe() for f in faults.active]

    @admin.post("/faults")
    async def add_fault(request: Request):
        try:
            fault = parse_fault(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        faults.add(fault)
        return [f.describe() for f in faults.active]

    @admin.delete("/faults")
    async def clear_faults():
        faults.clear()
        return []

    @admin.post("/market")
    async def set_market(request: Request):
        body = await request.json()
        await dispatch(wallet.set_kwh_enabled(bool(body.get("kwhEnabled"))))
        return {"kwhEnabled": wallet.kwh_enabled}

    @admin.post("/cycle")
    async def set_cycle(request: Request):
        wallet.cycle_id = (await request.json()).get("cycleId")
        return {"cycleId": wallet.cycle_id}

    @admin.get("/events")
    async def list_events(city: str | None = None, type: str | None = None, limit: int = 100):
        selected = [
            e
            for e in events
            if (city is None or e["city"] == city)
            and (type is None or e["message"]["type"] == type)
        ]
        return selected[-limit:]

    app.include_router(admin)
    return app
