from __future__ import annotations

import base64
import json

import pytest

from agent_rag.security.end_user import (
    IdentityAssertionError,
    decode_identity_assertion,
    encode_identity_assertion,
)
from agent_rag.security.models import EndUserIdentity

SECRET = b"internal-identity-secret-with-strong-entropy-0001"
OTHER_SECRET = b"attacker-controlled-secret-with-entropy-0000001"


def _identity() -> EndUserIdentity:
    return EndUserIdentity(
        subject="alice-123",
        tenant_id="faculty-engineering",
        groups=("researchers", "staff"),
        issuer="polyuquest-bff",
    )


def _token(*, now: int = 1_000, ttl: int = 60, audience: str = "polyuquest-api") -> str:
    return encode_identity_assertion(
        _identity(),
        SECRET,
        audience=audience,
        issuer="polyuquest-bff",
        ttl_seconds=ttl,
        now=now,
        token_id="assertion-123",
    )


def test_valid_identity_assertion_is_strictly_decoded() -> None:
    identity = decode_identity_assertion(
        _token(),
        SECRET,
        audience="polyuquest-api",
        issuer="polyuquest-bff",
        now=1_030,
    )

    assert identity.subject == "alice-123"
    assert identity.tenant_id == "faculty-engineering"
    assert identity.groups == ("researchers", "staff")
    assert identity.token_id == "assertion-123"


@pytest.mark.parametrize(
    ("token", "now"),
    [
        (_token(now=1_000, ttl=60), 1_100),
        (_token(now=1_100, ttl=60), 1_000),
        (_token(now=1_000, ttl=121), 1_010),
        (_token(now=1_000, audience="another-api"), 1_010),
    ],
)
def test_expired_future_overlong_and_cross_audience_tokens_fail(
    token: str, now: int
) -> None:
    with pytest.raises(IdentityAssertionError):
        decode_identity_assertion(
            token,
            SECRET,
            audience="polyuquest-api",
            issuer="polyuquest-bff",
            now=now,
        )


def test_forged_signature_and_algorithm_confusion_fail() -> None:
    forged = encode_identity_assertion(
        _identity(),
        OTHER_SECRET,
        audience="polyuquest-api",
        issuer="polyuquest-bff",
        now=1_000,
    )
    with pytest.raises(IdentityAssertionError):
        decode_identity_assertion(
            forged,
            SECRET,
            audience="polyuquest-api",
            issuer="polyuquest-bff",
            now=1_010,
        )

    parts = _token().split(".")
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "polyuquest-internal+jwt"}).encode()
    ).decode().rstrip("=")
    confused = ".".join((header, parts[1], parts[2]))
    with pytest.raises(IdentityAssertionError):
        decode_identity_assertion(
            confused,
            SECRET,
            audience="polyuquest-api",
            issuer="polyuquest-bff",
            now=1_010,
        )
