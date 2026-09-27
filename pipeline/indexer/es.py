"""Elasticsearch `_bulk` 호출과 항목별 결과 해석.

- action은 `index`, `_id = event_id`: 같은 event_id 재처리는 같은 문서를 덮어쓴다(중복 문서 없음).
- HTTP 200만 보고 성공으로 세지 않는다. 항목 수·순서·`_id`가 요청과 맞지 않으면 전체를 실패로 본다.
- 응답은 `filter_path`로 status·error.type·_id만 받는다.
  오류 reason에는 문서 값이 들어갈 수 있어 받지도 남기지도 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import requests

BULK_FILTER_PATH = "errors,items.*._id,items.*.status,items.*.error.type"
_ERROR_TYPE = re.compile(r"^[a-z0-9_]{1,64}$")


class BulkRequestError(Exception):
    """요청 전체가 실패했거나 응답을 신뢰할 수 없음. 어떤 항목도 ACK하지 않는다."""


@dataclass(frozen=True)
class BulkAction:
    index: str
    doc_id: str
    doc: dict[str, Any]


@dataclass(frozen=True)
class BulkItemResult:
    ok: bool
    status: int
    error_type: str | None


class BulkClient(Protocol):
    def bulk(self, actions: Sequence[BulkAction]) -> list[BulkItemResult]: ...


def build_bulk_body(actions: Sequence[BulkAction]) -> bytes:
    lines: list[str] = []
    for action in actions:
        lines.append(json.dumps({"index": {"_index": action.index, "_id": action.doc_id}}, separators=(",", ":")))
        lines.append(json.dumps(action.doc, separators=(",", ":"), ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_bulk_response(actions: Sequence[BulkAction], body: Any) -> list[BulkItemResult]:
    if not isinstance(body, dict):
        raise BulkRequestError("bulk response is not an object")
    items = body.get("items")
    if not isinstance(items, list) or len(items) != len(actions):
        raise BulkRequestError("bulk item count mismatch")
    results: list[BulkItemResult] = []
    for action, item in zip(actions, items, strict=True):
        if not isinstance(item, dict) or len(item) != 1 or "index" not in item:
            raise BulkRequestError("bulk item is not an index result")
        info = item["index"]
        if not isinstance(info, dict) or info.get("_id") != action.doc_id:
            raise BulkRequestError("bulk item _id mismatch")
        status = info.get("status")
        if not isinstance(status, int) or isinstance(status, bool):
            raise BulkRequestError("bulk item status missing")
        error = info.get("error")
        ok = 200 <= status < 300 and error is None
        error_type = None
        if not ok:
            raw_type = error.get("type") if isinstance(error, dict) else None
            error_type = raw_type if isinstance(raw_type, str) and _ERROR_TYPE.match(raw_type) else "unknown"
        results.append(BulkItemResult(ok=ok, status=status, error_type=error_type))
    return results


class ElasticsearchClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        ca_cert: str | None = None,
        timeout_s: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = (5.0, timeout_s)
        self._session = session or requests.Session()
        self._session.trust_env = False  # 프록시 env로 요청이 새지 않게 한다
        if api_key:
            self._session.headers["Authorization"] = "ApiKey " + api_key
        elif username and password:
            self._session.auth = (username, password)
        if ca_cert:
            self._session.verify = ca_cert

    def bulk(self, actions: Sequence[BulkAction]) -> list[BulkItemResult]:
        if not actions:
            return []
        try:
            resp = self._session.post(
                self._base + "/_bulk",
                params={"filter_path": BULK_FILTER_PATH},
                data=build_bulk_body(actions),
                headers={"Content-Type": "application/x-ndjson"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise BulkRequestError("transport " + type(exc).__name__) from None
        if resp.status_code != 200:
            raise BulkRequestError("http %d" % resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            raise BulkRequestError("bulk response is not JSON") from None
        return parse_bulk_response(actions, body)

    def put_index_template(self, name: str, template: dict[str, Any]) -> None:
        try:
            resp = self._session.put(
                self._base + "/_index_template/" + name,
                json=template,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise BulkRequestError("transport " + type(exc).__name__) from None
        if resp.status_code != 200:
            raise BulkRequestError("index template http %d" % resp.status_code)

    def index_template_exists(self, name: str) -> bool:
        try:
            resp = self._session.head(self._base + "/_index_template/" + name, timeout=self._timeout)
        except requests.RequestException as exc:
            raise BulkRequestError("transport " + type(exc).__name__) from None
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise BulkRequestError("index template http %d" % resp.status_code)
        return True
