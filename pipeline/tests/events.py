"""시험용 SecurityEvent v2 생성기. C-02 valid fixture를 틀로 쓰고 event_id·시각만 바꾼다."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "security-event" / "v2" / "fixtures"

_TEMPLATES = ("valid/api_read_allow.json", "valid/edge_completed.json", "valid/auth_login_success.json")


def _load(rel: str) -> dict[str, Any]:
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def format_utc(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += (".%06d" % value.microsecond).rstrip("0")
    return text + "Z"


def make_event(
    occurred_at: datetime | str | None = None, *, kind: int = 0, event_id: str | None = None
) -> dict[str, Any]:
    doc = copy.deepcopy(_load(_TEMPLATES[kind % len(_TEMPLATES)]))
    doc["event_id"] = event_id or str(uuid.uuid4())
    if doc.get("request_id") is not None:
        doc["request_id"] = str(uuid.uuid4())
    if occurred_at is None:
        occurred_at = datetime.now(timezone.utc)
    doc["occurred_at"] = occurred_at if isinstance(occurred_at, str) else format_utc(occurred_at)
    return doc


def invalid_fixture(name: str) -> dict[str, Any] | str:
    path = FIXTURES / "invalid" / name
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text
