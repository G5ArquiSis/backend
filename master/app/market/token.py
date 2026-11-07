"""Verificación del `swapToken` antes de aceptar un swap (rol D, tarea D4).

El token es un JWT ES256 que firma la wallet al proponerse el swap. Es lo que impide que un
vendedor (o alguien que se haga pasar por él) nos haga aceptar algo distinto de lo que
pedimos. Se verifica, como mínimo (Referencia API v3 § Token):

- la firma, contra las claves públicas de `/.well-known/jwks.json` de la wallet;
- `sub` = el swapId recibido;
- `proposer` = el vendedor al que le mandamos el bid;
- `give` = exactamente los kWh pedidos, y `take` = `round2(cantidad × precio)` en CR;
- `memo` = el offerId;
- `exp` en el futuro.

Las claves se guardan en memoria por `JWKS_TTL_SECONDS`; un `kid` desconocido fuerza una
recarga, por si la wallet rotó la clave.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx
import jwt

JWKS_TTL_SECONDS = 600


def jwks_url(wallet_base_url: str, override: str | None = None) -> str:
    """El JWKS cuelga de la raíz de la wallet, no de `/v1`."""
    if override:
        return override
    base = wallet_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/.well-known/jwks.json"


class JwksCache:
    def __init__(self, url: str, *, transport=None, ttl: float = JWKS_TTL_SECONDS):
        self.url = url
        self.ttl = ttl
        self._transport = transport
        self._keys: dict[str, dict] = {}
        self._loaded_at = 0.0

    async def key_for(self, kid: str | None) -> dict | None:
        expired = time.monotonic() - self._loaded_at > self.ttl
        if expired or kid not in self._keys:
            await self._load()
        if kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return self._keys.get(kid)

    async def _load(self) -> None:
        async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            keys = response.json().get("keys", [])
        self._keys = {key.get("kid"): key for key in keys if isinstance(key, dict)}
        self._loaded_at = time.monotonic()


_caches: dict[str, JwksCache] = {}


def cache_for(url: str) -> JwksCache:
    if url not in _caches:
        _caches[url] = JwksCache(url)
    return _caches[url]


@dataclass(frozen=True)
class Expected:
    """Lo que pedimos: contra esto se compara el token."""

    swap_id: str
    seller: str
    quantity: Decimal
    amount: Decimal
    offer_id: str


async def verify_swap_token(
    token: str, expected: Expected, jwks: JwksCache, now: datetime
) -> list[str]:
    """Devuelve los problemas del token; vacío si se puede aceptar.

    Un error al bajar el JWKS se propaga: no es un token inválido, es que todavía no se
    puede saber, y el comprador reintenta en el siguiente tick.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        return [f"token ilegible: {error}"]
    if header.get("alg") != "ES256":
        return [f"alg {header.get('alg')!r} no es ES256"]
    key = await jwks.key_for(header.get("kid"))
    if key is None:
        return [f"kid {header.get('kid')!r} no está en el JWKS"]
    try:
        claims = jwt.decode(
            token,
            jwt.PyJWK(key).key,
            algorithms=["ES256"],
            # exp se compara abajo contra `now` (el reloj del tick), no contra el de PyJWT.
            options={
                "verify_aud": False,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": ["exp", "sub"],
            },
        )
    except jwt.PyJWTError as error:
        return [f"firma inválida: {error}"]

    problems = []
    if claims.get("exp", 0) <= now.timestamp():
        problems.append("exp vencido")
    if claims.get("sub") != expected.swap_id:
        problems.append(f"sub {claims.get('sub')!r} no es el swapId {expected.swap_id!r}")
    if claims.get("proposer") != expected.seller:
        problems.append(f"proposer {claims.get('proposer')!r} no es {expected.seller!r}")
    give = claims.get("give") or {}
    if give.get("asset") != "KWH" or _amount(give.get("amount")) != expected.quantity:
        problems.append(f"give {give} no son {expected.quantity} KWH")
    take = claims.get("take") or {}
    if take.get("asset") != "CR" or _amount(take.get("amount")) != expected.amount:
        problems.append(f"take {take} no son {expected.amount} CR")
    if claims.get("memo") != expected.offer_id:
        problems.append(f"memo {claims.get('memo')!r} no es {expected.offer_id!r}")
    return problems


def _amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None
