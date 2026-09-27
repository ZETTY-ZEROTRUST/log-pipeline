"""`python -m pipeline.recovery {status|replay}`

운영 계정(MYSQL_* env, 권한: outbox SELECT + 상태 컬럼 UPDATE, receipt SELECT)으로 실행한다.

  status                                   PENDING / PUBLISHED / receipt 없는 PUBLISHED 행 수
  replay --mode unreceipted [--published-before-seconds S] [--dry-run]
      receipt가 없는 PUBLISHED 행을 PENDING으로 되돌린다(Redis 유실 복구의 기본).
      S초 이전에 발행된 행만 대상으로 해 진행 중인 전달과 겹치는 것을 줄인다(겹쳐도 event_id로 멱등).
  replay --mode all [--dry-run]
      모든 PUBLISHED 행을 PENDING으로 되돌린다(ES 재구축). receipt는 그대로 두고 ES 문서는 덮어쓴다.

relay가 PENDING 행을 다시 XADD하고, indexer는 group이 없으면 0부터 다시 만든다.
"""

from __future__ import annotations

import argparse
import logging
import sys

from pipeline.common.config import ConfigError, MySQLSettings
from pipeline.common.connections import connect_mysql
from pipeline.common.runtime import error_label, setup_logging

LOG = logging.getLogger("pipeline.recovery")

CHUNK = 500

STATUS_SQL = (
    "SELECT "
    "SUM(o.status = 'PENDING'), "
    "SUM(o.status = 'PUBLISHED'), "
    "SUM(o.status = 'PUBLISHED' AND r.event_id IS NULL) "
    "FROM security_event_outbox o LEFT JOIN security_event_receipt r ON r.event_id = o.event_id"
)

SELECT_UNRECEIPTED_SQL = (
    "SELECT o.id FROM security_event_outbox o "
    "LEFT JOIN security_event_receipt r ON r.event_id = o.event_id "
    "WHERE o.status = 'PUBLISHED' AND r.event_id IS NULL "
    "AND o.published_at <= UTC_TIMESTAMP(6) - INTERVAL %s SECOND AND o.id > %s "
    "ORDER BY o.id LIMIT %s"
)

SELECT_ALL_PUBLISHED_SQL = (
    "SELECT o.id FROM security_event_outbox o WHERE o.status = 'PUBLISHED' AND o.id > %s ORDER BY o.id LIMIT %s"
)

RESET_SQL = (
    "UPDATE security_event_outbox SET status = 'PENDING', lease_owner = NULL, lease_until = NULL "
    "WHERE id IN ({ids}) AND status = 'PUBLISHED'"
)


def _status(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(STATUS_SQL)
        pending, published, unreceipted = cur.fetchone()
    return {
        "pending": int(pending or 0),
        "published": int(published or 0),
        "published_without_receipt": int(unreceipted or 0),
    }


def _format_status(label: str, status: dict[str, int]) -> str:
    return "%s %s" % (label, " ".join("%s=%d" % (k, v) for k, v in status.items()))


def _replay(conn, mode: str, published_before_s: int, dry_run: bool) -> int:
    last_id, total = 0, 0
    while True:
        with conn.cursor() as cur:
            if mode == "unreceipted":
                cur.execute(SELECT_UNRECEIPTED_SQL, (published_before_s, last_id, CHUNK))
            else:
                cur.execute(SELECT_ALL_PUBLISHED_SQL, (last_id, CHUNK))
            ids = [int(row[0]) for row in cur.fetchall()]
        if not ids:
            return total
        last_id = ids[-1]
        if dry_run:
            total += len(ids)
            continue
        # chunk마다 짧게 commit해 relay의 lease 획득을 오래 막지 않는다.
        with conn.cursor() as cur:
            total += cur.execute(RESET_SQL.format(ids=", ".join(["%s"] * len(ids))), ids)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="python -m pipeline.recovery", description="Outbox replay 운영 명령")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Outbox/receipt 상태 건수")
    replay = sub.add_parser("replay", help="PUBLISHED 행을 PENDING으로 되돌려 재발행")
    replay.add_argument("--mode", choices=("unreceipted", "all"), default="unreceipted")
    replay.add_argument("--published-before-seconds", type=int, default=60)
    replay.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        settings = MySQLSettings.from_env()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    try:
        conn = connect_mysql(settings)
    except Exception as exc:  # noqa: BLE001
        LOG.error("mysql connect failed error=%s", error_label(exc))
        return 1
    try:
        before = _status(conn)
        print(_format_status("before", before))
        if args.command == "replay":
            count = _replay(conn, args.mode, max(0, args.published_before_seconds), args.dry_run)
            action = "would reset" if args.dry_run else "reset"
            print("replay mode=%s %s=%d" % (args.mode, action.replace(" ", "_"), count))
            after = _status(conn)
            print(_format_status("after", after))
    except Exception as exc:  # noqa: BLE001
        LOG.error("recovery failed error=%s", error_label(exc))
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
