"""Strict short-lived JWT assertions for trusted BFF-to-API identity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status

from agent_rag.config import settings
from agent_rag.security.models import LEGACY_END_USER, EndUserIdentity

IDENTITY_HEADER = "X-PolyUQuest-Identity"
JWT_TYPE = "polyuquest-internal+jwt"
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_GROUP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,63}$")


class IdentityAssertionError(ValueError):
    """The identity assertion is missing, malformed, or not trustworthy."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise IdentityAssertionError("invalid identity assertion")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise IdentityAssertionError("invalid identity assertion") from exc


def _strict_json(value: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise IdentityAssertionError("invalid identity assertion")
            result[key] = item
        return result

    try:
        decoded = value.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityAssertionError("invalid identity assertion") from exc
    if not isinstance(parsed, dict):
        raise IdentityAssertionError("invalid identity assertion")
    return parsed


def load_identity_secret(path: str) -> bytes:
    selected = Path(path)
    try:
        secret = selected.read_text(encoding="utf-8").strip().encode("utf-8")
    except OSError as exc:
        raise IdentityAssertionError("identity verifier is not configured") from exc
    if len(secret) < 32 or len(secret) > 512:
        raise IdentityAssertionError("identity verifier is not configured")
    return secret


def encode_identity_assertion(
    identity: EndUserIdentity,
    secret: bytes,
    *,
    audience: str,
    issuer: str,
    ttl_seconds: int = 60,
    now: int | None = None,
    token_id: str = "test-token",
) -> str:
    """Create an assertion for tests and trusted gateway/BFF integrations."""
    issued_at = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": JWT_TYPE}
    payload = {
        "v": 1,
        "iss": issuer,
        "aud": audience,
        "sub": identity.subject,
        "tenant_id": identity.tenant_id,
        "groups": list(identity.groups),
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": token_id,
    }
    encoded = ".".join(
        _b64encode(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, payload)
    )
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def decode_identity_assertion(
    token: str,
    secret: bytes,
    *,
    audience: str,
    issuer: str,
    max_ttl_seconds: int = 120,
    clock_skew_seconds: int = 5,
    now: int | None = None,
) -> EndUserIdentity:
    if not token or len(token) > 4096 or not _TOKEN.fullmatch(token):
        raise IdentityAssertionError("invalid identity assertion")
    encoded_header, encoded_payload, encoded_signature = token.split(".")
    header = _strict_json(_b64decode(encoded_header))
    if header != {"alg": "HS256", "typ": JWT_TYPE}:
        raise IdentityAssertionError("invalid identity assertion")
    expected = hmac.new(
        secret,
        f"{encoded_header}.{encoded_payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    supplied = _b64decode(encoded_signature)
    if not hmac.compare_digest(expected, supplied):
        raise IdentityAssertionError("invalid identity assertion")
    claims = _strict_json(_b64decode(encoded_payload))
    required = {"v", "iss", "aud", "sub", "tenant_id", "groups", "iat", "exp", "jti"}
    if set(claims) != required or claims.get("v") != 1:
        raise IdentityAssertionError("invalid identity assertion")
    if claims.get("iss") != issuer or claims.get("aud") != audience:
        raise IdentityAssertionError("invalid identity assertion")
    if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
        raise IdentityAssertionError("invalid identity assertion")
    issued_at, expires_at = claims["iat"], claims["exp"]
    selected_now = int(time.time() if now is None else now)
    if (
        expires_at <= issued_at
        or expires_at - issued_at > max_ttl_seconds
        or issued_at > selected_now + clock_skew_seconds
        or expires_at < selected_now - clock_skew_seconds
    ):
        raise IdentityAssertionError("invalid identity assertion")
    subject = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    token_id = claims.get("jti")
    groups = claims.get("groups")
    if (
        not isinstance(subject, str)
        or not _IDENTIFIER.fullmatch(subject)
        or not isinstance(tenant_id, str)
        or not _IDENTIFIER.fullmatch(tenant_id)
        or not isinstance(token_id, str)
        or not _IDENTIFIER.fullmatch(token_id)
        or not isinstance(groups, list)
        or len(groups) > 32
        or any(not isinstance(group, str) or not _GROUP.fullmatch(group) for group in groups)
        or len(set(groups)) != len(groups)
    ):
        raise IdentityAssertionError("invalid identity assertion")
    return EndUserIdentity(
        subject=subject,
        tenant_id=tenant_id,
        groups=tuple(groups),
        issuer=issuer,
        token_id=token_id,
    )


async def require_end_user_identity(
    assertion: Annotated[str | None, Header(alias=IDENTITY_HEADER)] = None,
) -> EndUserIdentity:
    if settings.end_user_identity_mode == "disabled":
        return LEGACY_END_USER
    try:
        secret = load_identity_secret(settings.end_user_identity_secret_file)
        return decode_identity_assertion(
            assertion or "",
            secret,
            audience=settings.end_user_identity_audience,
            issuer=settings.end_user_identity_issuer,
            max_ttl_seconds=settings.end_user_identity_max_ttl_seconds,
            clock_skew_seconds=settings.end_user_identity_clock_skew_seconds,
        )
    except IdentityAssertionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid end-user identity",
        ) from exc


EndUser = Annotated[EndUserIdentity, Depends(require_end_user_identity)]
