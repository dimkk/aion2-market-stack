from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from datetime import UTC, datetime

from .models import User
from .postgres_repository import PostgresConnection, PostgresRepository
from .security import SecurityError, hash_password, normalize_username, validate_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot PostgreSQL admin bootstrap; password input is never an argv value."
    )
    parser.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    try:
        username = normalize_username(args.username)
        password = validate_password(getpass.getpass("Initial admin password: "))
        confirmation = getpass.getpass("Repeat password: ")
    except SecurityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if password != confirmation:
        print("passwords_do_not_match", file=sys.stderr)
        return 2
    repository = PostgresRepository(PostgresConnection(os.environ["POSTGRES_DSN"]))
    now = datetime.now(UTC)
    created = repository.create_user(
        User(
            username=username,
            user_id=str(uuid.uuid4()),
            password_hash=hash_password(password),
            role="admin",
            created_at=now,
            updated_at=now,
        )
    )
    print("admin_created" if created else "admin_already_exists")
    return 0 if created else 1


if __name__ == "__main__":
    raise SystemExit(main())
