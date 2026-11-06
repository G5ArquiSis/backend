"""Cliente asíncrono v3: escrituras idempotentes y lecturas con límite local."""
import asyncio
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class WalletError(Exception):
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Wallet HTTP {status_code}: {payload}")


class RateLimiter:

    "clase para controlar/limitar las lecturas y escrituras a la wallet por minuto."

    def __init__(self, maximum: int):
        self.maximum = maximum
        self._timestamps = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.maximum:
                    self._timestamps.append(now)
                    return
                await asyncio.sleep(max(0.01, 60 - (now - self._timestamps[0])))


def retry_after(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback


class WalletClient:
    """
    Un HTTP 202 sólo acusa recepción, nunca implica swap liquidado.

    Para escribir en la wallet, se necesita un Idempotency-Key (idpk) único por operación.
    La misma clave se conserva en cada reintento
    El resultado final de la transaccion llega despues como un evento a la cola city.code
    """

    _shared_limiters: dict[tuple[str, str],
                           tuple[RateLimiter, RateLimiter]] = {}

    def __init__(self, base_url: str, api_key: str, *, transport=None,
                 timeout: float = 20.0, max_attempts: int = 4,
                 hook: Callable[[str, str, int | None, float], None] | None = None):

        if not base_url or not api_key:
            raise ValueError(
                "WALLET_BASE_URL y WALLET_API_KEY son obligatorias")

        self._client = httpx.AsyncClient(base_url=base_url.rstrip('/') + '/',
                                         headers={'X-Api-Key': api_key},
                                         timeout=timeout, transport=transport)

        # maximo de 120 escrituras por minuto en la wallet
        self._writes = RateLimiter(120)
        # maximo de 300 lecturas por minuto en la wallet
        self._reads = RateLimiter(300)
        self.max_attempts = max(1, max_attempts)
        self.hook = hook

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def _request(self, method: str, path: str, *, idempotency_key=None,
                       json=None, params=None):

        if method == 'POST' and not idempotency_key:
            raise ValueError(
                'Idempotency-Key es obligatoria para escrituras en la wallet')

        headers = {
            'Idempotency-Key': str(idempotency_key)} if method == 'POST' else {}

        for attempt in range(self.max_attempts):

            await (self._writes if method == 'POST' else self._reads).acquire()
            start = time.monotonic()
            status = None

            try:
                response = await self._client.request(method, path.lstrip('/'),
                                                      headers=headers, json=json, params=params)
                status = response.status_code

                if status in (429, 502, 503, 504) and attempt + 1 < self.max_attempts:
                    delay = retry_after(response.headers.get(
                        'Retry-After'), min(2 ** attempt, 8))

                else:
                    if status >= 400:
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = response.text
                        raise WalletError(status, payload)
                    return response.json()

            except (httpx.TimeoutException, httpx.TransportError):
                if attempt + 1 == self.max_attempts:
                    raise
                delay = min(2 ** attempt, 8)

            finally:
                if self.hook:
                    self.hook(method, path, status, time.monotonic() - start)
            await asyncio.sleep(delay)

        raise RuntimeError('Reintentos agotados')

    async def propose_swap(self, give: dict, take: dict, expires_at: str,
                           memo: str, idempotency_key: str):

        # give es un dict con asset (el activo es la energía) y amount (cantidad de kwh que ofrece)
        # take es un dict con asset (el activo es el credito) y amount (lo que cuesta en total
        # aceptar el swap).
        # Ejemplo: give = {"asset": "kwh", "amount": 10}, take = {"asset": "credits", "amount": 100}
        # lo que se retiene es el Give (kwh) de la cuenta del proponente

        json_propose = {'give': give, 'take': take,
                        'expiresAt': expires_at, 'memo': memo}

        return await self._request('POST', 'swaps', idempotency_key=idempotency_key,
                                   json=json_propose)

    async def accept_swap(self, swap_id: str, idempotency_key: str):

        # se aplica cuando el swap propuesto es aceptado por alguna ciudad.
        # se retiene el take (creditos) de la cuenta del comprador.

        return await self._request('POST', f'swaps/{swap_id}/accept',
                                   idempotency_key=idempotency_key)

    async def cancel_swap(self, swap_id: str, idempotency_key: str):

        # cancela una propuesta no aceptada por nadie.
        # se libera la retención del give (kwh) de la cuenta del proponente.

        return await self._request('POST', f'swaps/{swap_id}/cancel',
                                   idempotency_key=idempotency_key)

    # lecturas de la wallet (no requieren idempotency key, no retienen nada, no generan eventos):

    async def get_swap(self, swap_id: str):

        # se muestra el estado del swap: accepted, proposed, settled, cancelled,
        # expired, rejected, y el último evento.

        # muestra en lastEvent.data.balancesAfter los saldos propios de quien lee
        # (quien tiene el swapId), en el instante de la lectura, nunca los de otra ciudad.

        return await self._request('GET', f'swaps/{swap_id}')

    get_swap_id = get_swap

    async def get_balance(self):

        # entrega el json con los saldos de kwh y creditos de la ciudad que hace la lectura,
        # en el instante de la lectura.

        return await self._request('GET', 'balance')

    async def get_statement(self, *, cycle_id=None, asset=None, cursor=None, limit=None):

        # es para consultar los movimientos de la wallet de la ciudad que hace la lectura,
        # en el instante de la lectura.

        params = {'cycleId': cycle_id, 'asset': asset,
                  'cursor': cursor, 'limit': limit}
        return await self._request('GET', 'statement', params={k: v for k, v in params.items()
                                                               if v is not None})

    async def get_leaderboard(self):

        # es para consultar la clasificación de ciudades

        return await self._request('GET', 'leaderboard')
