"""Auth middleware tests.

These are the canary — they exercise every JWT rejection path with a
real RSA keypair so a regression in `auth.py` fails loudly. If you
change that file, these tests must still pass.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from homelab_mcp.auth import JWTAuthMiddleware

# ── helpers ──────────────────────────────────────────────────────────


def _b64url_uint(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _make_jwks(pubkey: rsa.RSAPublicKey, kid: str = "test-kid") -> dict[str, Any]:
    numbers = pubkey.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


def _make_token(
    privkey: RSAPrivateKey,
    *,
    iss: str,
    aud: str,
    exp_offset: int = 300,
    kid: str = "test-kid",
    email: str = "test@example.com",
) -> str:
    pem = privkey.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    return jwt.encode(
        {
            "iss": iss,
            "aud": aud,
            "exp": now + exp_offset,
            "iat": now,
            "email": email,
        },
        pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


class StubJWKS:
    """Stand-in for JWKSCache that returns a fixed dict."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self.jwks = jwks

    async def get(self) -> dict[str, Any]:
        return self.jwks


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def keypair() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(keypair: RSAPrivateKey) -> dict[str, Any]:
    return _make_jwks(keypair.public_key())


@pytest.fixture
def issuer() -> str:
    return "https://test.cloudflareaccess.com"


@pytest.fixture
def audience() -> str:
    return "test-audience-tag"


# ── call harness ─────────────────────────────────────────────────────


async def _call(
    middleware_init_kwargs: dict[str, Any],
    *,
    token: str | None = None,
    header_key: bytes = b"authorization",
    scope_type: str = "http",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drive the middleware with a synthetic ASGI scope and capture send() calls."""
    sent: list[dict[str, Any]] = []
    inner_called: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def inner_app(scope: dict[str, Any], _r: Awaitable[Any], snd: Any) -> None:
        inner_called.append(scope)
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"ok"})

    headers: list[tuple[bytes, bytes]] = []
    if token:
        if header_key == b"authorization":
            headers.append((b"authorization", f"Bearer {token}".encode()))
        else:
            headers.append((header_key, token.encode()))

    mw = JWTAuthMiddleware(inner_app, **middleware_init_kwargs)
    await mw({"type": scope_type, "headers": headers}, receive, send)
    return sent, inner_called


@pytest.fixture
def mw_kwargs(jwks: dict[str, Any], issuer: str, audience: str) -> dict[str, Any]:
    return {"jwks_cache": StubJWKS(jwks), "issuer": issuer, "audience": audience}


# ── tests ────────────────────────────────────────────────────────────


async def test_valid_bearer_token_passes(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    issuer: str,
    audience: str,
) -> None:
    token = _make_token(keypair, iss=issuer, aud=audience)
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 1
    assert sent[0]["status"] == 200
    assert inner[0]["user"]["email"] == "test@example.com"


async def test_valid_cf_access_header_passes(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    issuer: str,
    audience: str,
) -> None:
    token = _make_token(keypair, iss=issuer, aud=audience)
    sent, inner = await _call(mw_kwargs, token=token, header_key=b"cf-access-jwt-assertion")
    assert len(inner) == 1
    assert sent[0]["status"] == 200


async def test_missing_token_returns_401(mw_kwargs: dict[str, Any]) -> None:
    sent, inner = await _call(mw_kwargs, token=None)
    assert len(inner) == 0
    assert sent[0]["status"] == 401
    assert b"missing" in sent[1]["body"].lower()


async def test_expired_token_returns_401(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    issuer: str,
    audience: str,
) -> None:
    token = _make_token(keypair, iss=issuer, aud=audience, exp_offset=-60)
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_wrong_audience_returns_401(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    issuer: str,
) -> None:
    token = _make_token(keypair, iss=issuer, aud="wrong-audience")
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_wrong_issuer_returns_401(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    audience: str,
) -> None:
    token = _make_token(keypair, iss="https://other.cloudflareaccess.com", aud=audience)
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_unknown_kid_returns_401(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    issuer: str,
    audience: str,
) -> None:
    token = _make_token(keypair, iss=issuer, aud=audience, kid="other-kid")
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_signature_from_different_key_returns_401(
    mw_kwargs: dict[str, Any],
    issuer: str,
    audience: str,
) -> None:
    """JWT signed by a different key but advertising the same kid should fail."""
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token(impostor, iss=issuer, aud=audience)
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_garbage_token_returns_401(mw_kwargs: dict[str, Any]) -> None:
    sent, inner = await _call(mw_kwargs, token="not-a-jwt")
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_non_http_scope_passes_through(mw_kwargs: dict[str, Any]) -> None:
    """Lifespan / websocket scopes must NOT be challenged."""
    sent, inner = await _call(mw_kwargs, token=None, scope_type="lifespan")
    # Inner app is invoked
    assert len(inner) == 1
