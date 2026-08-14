"""Generate a one-time API key and its repository-safe hash specification."""

from __future__ import annotations

import argparse
import secrets

from agent_rag.security.credentials import hash_api_key, parse_api_key_records
from agent_rag.security.models import Role


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--role", choices=[role.value for role in Role], required=True)
    args = parser.parse_args()
    secret = secrets.token_urlsafe(32)
    record = f"{args.key_id}:{args.role}:{hash_api_key(secret)}"
    try:
        parse_api_key_records(record, require_admin=False)
    except ValueError as exc:
        parser.error(str(exc))
    print("Store this raw key in the caller's secret manager; it is shown once:")
    print(secret)
    print("Add this hash-only entry to API_AUTH_KEYS:")
    print(record)


if __name__ == "__main__":
    main()
