"""Auth middleware tests.

These are the canary — they exercise every JWT rejection path with a
real RSA keypair so a regression in `auth.py` fails loudly. If you
change that file, these tests must still pass.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any

import jwt
import pytest
from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from homelab_mcp.auth import JWTAuthMiddleware
from homelab_mcp.signing_key import SigningKey

# ── helpers ──────────────────────────────────────────────────────────


def _signing_key_from(privkey: RSAPrivateKey, kid: str = "test-kid") -> SigningKey:
    """Wrap a raw RSA private key in the SigningKey dataclass the middleware expects."""
    pem = privkey.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = privkey.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "use": "sig", "alg": "RS256"})
    public_dict: dict[str, Any] = dict(public_jwk.as_dict())
    public_dict["kid"] = kid
    return SigningKey(private_pem=pem, kid=kid, public_jwk=public_dict)


def _make_token(
    privkey: RSAPrivateKey,
    *,
    iss: str,
    aud: str,
    exp_offset: int = 300,
    kid: str = "test-kid",
    email: str = "test@example.com",
    sub: str | None = None,
) -> str:
    pem = privkey.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "exp": now + exp_offset,
        "iat": now,
        "nbf": now,
        "sub": sub or email,
        "email": email,
    }
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def keypair() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_key(keypair: RSAPrivateKey) -> SigningKey:
    return _signing_key_from(keypair)


@pytest.fixture
def issuer() -> str:
    return "https://mcp.example.com"


@pytest.fixture
def audience() -> str:
    return "https://mcp.example.com"


@pytest.fixture
def mw_kwargs(signing_key: SigningKey, issuer: str, audience: str) -> dict[str, Any]:
    return {"signing_key": signing_key, "issuer": issuer, "audience": audience}


# ── call harness ─────────────────────────────────────────────────────


async def _call(
    middleware_init_kwargs: dict[str, Any],
    *,
    token: str | None = None,
    scope_type: str = "http",
    path: str = "/mcp",
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
        headers.append((b"authorization", f"Bearer {token}".encode()))

    mw = JWTAuthMiddleware(inner_app, **middleware_init_kwargs)
    await mw({"type": scope_type, "headers": headers, "path": path}, receive, send)
    return sent, inner_called


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


async def test_cf_access_header_no_longer_accepted(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    issuer: str,
    audience: str,
) -> None:
    """We removed CF Access integration; only Authorization: Bearer is accepted."""
    token = _make_token(keypair, iss=issuer, aud=audience)
    sent: list[dict[str, Any]] = []
    inner_called: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def inner_app(scope: dict[str, Any], _r: Awaitable[Any], snd: Any) -> None:
        inner_called.append(scope)

    mw = JWTAuthMiddleware(
        lambda *a, **k: inner_app(*a, **k),  # type: ignore[arg-type]
        **mw_kwargs,
    )
    await mw(
        {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"cf-access-jwt-assertion", token.encode())],
        },
        receive,
        send,
    )
    assert len(inner_called) == 0
    assert sent[0]["status"] == 401


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
    token = _make_token(keypair, iss=issuer, aud="https://wrong.example.com")
    sent, inner = await _call(mw_kwargs, token=token)
    assert len(inner) == 0
    assert sent[0]["status"] == 401


async def test_wrong_issuer_returns_401(
    mw_kwargs: dict[str, Any],
    keypair: RSAPrivateKey,
    audience: str,
) -> None:
    token = _make_token(keypair, iss="https://other.example.com", aud=audience)
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
    assert len(inner) == 1


async def test_unauthenticated_paths_passthrough(
    mw_kwargs: dict[str, Any],
) -> None:
    """Discovery + OAuth endpoints must not require auth."""
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/oauth/jwks.json",
        "/oauth/register",
        "/oauth/authorize",
        "/oauth/callback",
        "/oauth/token",
    ):
        sent, inner = await _call(mw_kwargs, token=None, path=path)
        assert len(inner) == 1, f"path {path} should pass through without auth"
        assert sent[0]["status"] == 200, f"path {path} got {sent[0]['status']}"
