"""Identity at the edge.

A principal is minted from a signed token and is never read from a request body.
That is the whole point: the tenant a caller acts in, and whether they are a
human or a service, must be something the platform asserts, not something the
caller claims.

Tokens here are HMAC-signed and self-contained, which is enough to make the
boundary real and testable. Swapping in OIDC replaces `resolve` and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from aios.platform.models import Principal, PrincipalKind


class InvalidToken(PermissionError):
    """The token was missing, malformed, unsigned or expired."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class TokenIssuer:
    """Mints and verifies the tokens the API accepts."""

    def __init__(self, secret: str, ttl_seconds: int = 86_400) -> None:
        self._secret = secret.encode()
        self._ttl = ttl_seconds

    def mint(
        self,
        tenant_id: str,
        subject: str,
        kind: PrincipalKind,
        scopes: tuple[str, ...] = (),
    ) -> str:
        claims = {
            "tenant_id": tenant_id,
            "sub": subject,
            "kind": kind.value,
            "scopes": list(scopes),
            "exp": int(time.time()) + self._ttl,
        }
        payload = _b64(json.dumps(claims, sort_keys=True).encode())
        return f"{payload}.{self._sign(payload)}"

    def resolve(self, token: str | None) -> Principal:
        """Verify a token and return the principal it names."""
        if not token:
            raise InvalidToken("no token supplied")
        token = token.removeprefix("Bearer ").strip()
        payload, _, signature = token.partition(".")
        if not signature or not hmac.compare_digest(signature, self._sign(payload)):
            raise InvalidToken("signature does not verify")
        try:
            claims = json.loads(_unb64(payload))
        except Exception as error:
            raise InvalidToken(f"malformed token: {error}") from error
        if claims.get("exp", 0) < time.time():
            raise InvalidToken("token expired")
        return Principal(
            tenant_id=claims["tenant_id"],
            subject=claims["sub"],
            kind=PrincipalKind(claims["kind"]),
            scopes=tuple(claims.get("scopes", ())),
        )

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())
