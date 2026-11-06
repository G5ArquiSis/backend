"""Firma ES256 de los tokens de swap y el JWKS para verificarlos.

La llave se genera al arrancar (los tokens dejan de verificar si la wallet falsa se
reinicia) salvo que `FAKE_WALLET_SIGNING_KEY` apunte a un PEM EC P-256.
"""

from __future__ import annotations

import base64
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "wallet.energyshark"


def _b64url(number: int) -> str:
    return base64.urlsafe_b64encode(number.to_bytes(32, "big")).rstrip(b"=").decode()


class Signer:
    def __init__(self, kid: str = "fake-wallet-1", pem_path: str | None = None):
        if pem_path:
            key = serialization.load_pem_private_key(Path(pem_path).read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise ValueError("FAKE_WALLET_SIGNING_KEY debe ser una llave EC P-256")
            self.key = key
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
        self.kid = kid

    def sign_swap(self, swap) -> str:
        claims = {
            "iss": ISSUER,
            "sub": swap.swap_id,
            "iat": int(swap.created_at.timestamp()),
            "exp": int(swap.expires_at.timestamp()),
            "proposer": swap.proposer,
            "give": swap.give.out(),
            "take": swap.take.out(),
            "memo": swap.memo,
        }
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})

    def jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "alg": "ES256",
                    "use": "sig",
                    "kid": self.kid,
                    "x": _b64url(numbers.x),
                    "y": _b64url(numbers.y),
                }
            ]
        }
