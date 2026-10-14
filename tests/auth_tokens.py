"""Signed test tokens and a controllable key set, shared by platform and API tests."""

import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

ISSUER = "https://project.supabase.co/auth/v1"
AUDIENCE = "authenticated"
USER_ID = uuid.UUID("11111111-0000-0000-0000-000000000001")


class SigningKey:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = ec.generate_private_key(ec.SECP256R1())

    def jwk(self) -> dict[str, Any]:
        public = ECAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return {**public, "kid": self.kid, "alg": "ES256", "use": "sig"}

    def sign(self, claims: Mapping[str, Any], *, kid: str | None = None) -> str:
        return jwt.encode(
            dict(claims), self.private, algorithm="ES256", headers={"kid": kid or self.kid}
        )


class FakeJwks:
    """A key set the test controls, counting fetches."""

    def __init__(self, *keys: SigningKey) -> None:
        self.keys = list(keys)
        self.fetches = 0
        self.fail = False

    async def fetch(self) -> Mapping[str, Any]:
        self.fetches += 1
        if self.fail:
            raise httpx.ConnectError("auth provider unreachable")
        return {"keys": [key.jwk() for key in self.keys]}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def claims(**overrides: object) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(USER_ID),
        "role": "authenticated",
        "aal": "aal1",
        "session_id": "session-1",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}
