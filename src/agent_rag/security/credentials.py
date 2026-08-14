"""Hashed API-key parsing kept independent from application configuration."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from agent_rag.security.models import Role

_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    role: Role
    secret_hash: str


def hash_api_key(secret: str) -> str:
    if not secret:
        raise ValueError("API key must not be empty")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_api_key_records(raw: str, *, require_admin: bool = True) -> list[ApiKeyRecord]:
    records: list[ApiKeyRecord] = []
    for position, entry in enumerate(raw.split(","), start=1):
        selected = entry.strip()
        if not selected:
            continue
        parts = selected.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"API_AUTH_KEYS entry {position} must be key_id:role:sha256_hex"
            )
        key_id, role_raw, secret_hash = (part.strip() for part in parts)
        if not _KEY_ID.fullmatch(key_id):
            raise ValueError(f"API_AUTH_KEYS entry {position} has an invalid key_id")
        try:
            role = Role(role_raw)
        except ValueError as exc:
            raise ValueError(
                f"API_AUTH_KEYS entry {position} has an invalid role"
            ) from exc
        if not _SHA256.fullmatch(secret_hash):
            raise ValueError(
                f"API_AUTH_KEYS entry {position} must contain a lowercase SHA-256 digest"
            )
        records.append(ApiKeyRecord(key_id, role, secret_hash))
    if not records:
        raise ValueError("API_AUTH_MODE=api_key requires at least one API_AUTH_KEYS entry")
    key_ids = [record.key_id for record in records]
    hashes = [record.secret_hash for record in records]
    if len(key_ids) != len(set(key_ids)):
        raise ValueError("API_AUTH_KEYS contains duplicate key_id values")
    if len(hashes) != len(set(hashes)):
        raise ValueError("API_AUTH_KEYS contains duplicate secret digests")
    if require_admin and not any(record.role is Role.admin for record in records):
        raise ValueError("API_AUTH_KEYS requires at least one admin key")
    return records
