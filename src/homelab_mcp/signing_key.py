"""RSA signing key loader for the OAuth provider.

The MCP server signs every access token with an RSA private key. Clients
(and our own JWT middleware) verify against the matching public key
published at /oauth/jwks.json.

Three sourcing paths, in order of precedence:

  1. `settings.oauth_signing_key` — PEM bytes directly in an env var
     (typically sops-managed). Preferred for production: the key never
     touches disk.

  2. `settings.oauth_signing_key_path` — PEM file. Loaded if it exists.

  3. Auto-generate a fresh 2048-bit RSA key and write it to
     `oauth_signing_key_path` (mode 0600). The directory must exist
     (NixOS module's StateDirectory handles this).

The result is exposed as both a private-key handle (for signing) and a
JWK dict (for the public side of the JWKS endpoint).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from homelab_mcp.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SigningKey:
    """A loaded RSA signing key plus its public JWK for the JWKS endpoint."""

    private_pem: bytes
    """PEM-encoded private key, kept for authlib's JsonWebKey signer."""

    kid: str
    """Stable key id surfaced in JWT headers and the JWKS doc."""

    public_jwk: dict[str, Any]
    """Public-only JWK dict suitable for serving at /oauth/jwks.json."""


def load_or_create(settings: Settings) -> SigningKey:
    """Resolve the signing key per the precedence rules in the module docstring.

    Raises:
        OSError: if neither sources are usable AND auto-generation can't
            write to the configured path.
    """
    if settings.oauth_signing_key:
        log.info("OAuth signing key: loaded from HOMELAB_MCP_OAUTH_SIGNING_KEY env var")
        pem = settings.oauth_signing_key.encode()
        return _build_from_pem(pem)

    path = Path(settings.oauth_signing_key_path)
    if path.exists():
        log.info("OAuth signing key: loaded from %s", path)
        return _build_from_pem(path.read_bytes())

    # Generate + persist.
    log.warning(
        "OAuth signing key: %s missing, generating fresh RSA-2048 key. "
        "Set HOMELAB_MCP_OAUTH_SIGNING_KEY (sops) to make this deterministic.",
        path,
    )
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive perms before populating, in case of crash mid-write.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    log.info("OAuth signing key persisted to %s (mode 0600)", path)
    return _build_from_pem(pem)


def _build_from_pem(pem: bytes) -> SigningKey:
    """Construct the SigningKey dataclass from a private-key PEM blob."""
    private: RSAPrivateKey = serialization.load_pem_private_key(pem, password=None)  # type: ignore[assignment]
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError(
            "OAuth signing key must be RSA. Other key types are not supported "
            "because authlib's JWT signers and clients we care about (Claude) "
            "negotiate RS256."
        )

    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # JsonWebKey.import_key returns an object with as_dict() we can serve.
    public_jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "use": "sig", "alg": "RS256"})
    # Derive the `kid` from the key's RFC 7638 thumbprint rather than a static
    # constant. This makes the key id change whenever the key material does
    # (e.g. an accidental regeneration when the on-disk key is lost), so a
    # verifier caching JWKS by `kid` gets a cache-miss + refetch instead of
    # silently validating against a stale key. Sign + verify both read this
    # same value, so they stay consistent within a process.
    kid = public_jwk.thumbprint()
    public_dict: dict[str, Any] = dict(public_jwk.as_dict())
    public_dict["kid"] = kid

    return SigningKey(
        private_pem=pem,
        kid=kid,
        public_jwk=public_dict,
    )
