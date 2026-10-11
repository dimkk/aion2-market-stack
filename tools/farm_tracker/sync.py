"""Fail-closed upload of aggregate farm sessions to the self-hosted API."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .history import HistoryStore


RECEIPT_SCHEMA = "a2.farm.sync-receipt.v1"
SAFE_RECEIPT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _endpoint(origin: str) -> str:
    parsed = urlsplit(origin)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not loopback)
    ):
        raise ValueError("farm_sync_origin_invalid")
    return origin.rstrip("/") + "/api/v1/desktop/farm/sessions"


def sync_session(
    history: HistoryStore,
    session_id: str,
    origin: str,
    token: str,
    *,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    if not token or len(token) > 4_096 or any(character.isspace() for character in token):
        raise ValueError("farm_sync_token_invalid")
    payload = history.export(session_id)
    payload_id = str(payload["payload_id"])
    existing = history.sync_receipt(payload_id)
    if existing is not None:
        return {
            "schema": RECEIPT_SCHEMA,
            "payload_id": payload_id,
            "accepted": True,
            "receipt_id": existing,
            "reused": True,
        }
    request = Request(
        _endpoint(origin),
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": payload_id,
        },
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise ValueError("farm_sync_rejected")
            raw = response.read(64 * 1024 + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        history.record_sync(session_id, payload_id, None, "farm_sync_unavailable")
        raise ValueError("farm_sync_unavailable") from error
    if len(raw) > 64 * 1024:
        raise ValueError("farm_sync_receipt_invalid")
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("farm_sync_receipt_invalid") from error
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema", "payload_id", "accepted", "receipt_id"}
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("payload_id") != payload_id
        or receipt.get("accepted") is not True
        or not isinstance(receipt.get("receipt_id"), str)
        or SAFE_RECEIPT.fullmatch(receipt["receipt_id"]) is None
    ):
        history.record_sync(session_id, payload_id, None, "farm_sync_receipt_invalid")
        raise ValueError("farm_sync_receipt_invalid")
    history.record_sync(session_id, payload_id, receipt["receipt_id"], None)
    return dict(receipt, reused=False)
