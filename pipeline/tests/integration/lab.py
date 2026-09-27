"""통합 시험 실험실: run.sh가 띄운 일회용 MySQL·Redis·ES와 relay/indexer 이미지를 다룬다.

- 시험 코드는 호스트(127.0.0.1 임시 포트)에서 root/admin 계정으로 준비·검증만 한다.
- relay/indexer는 실제 이미지를 컨테이너로 실행하고 최소 권한 계정(파일 secret)으로 접속한다.
- 비밀번호는 run.sh가 실행마다 만든 임시 파일에만 있고 출력하지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pymysql
import redis
import requests

PIPELINE_DIR = Path(__file__).resolve().parents[2]
SCHEMA_SQL = Path(__file__).resolve().parent / "sql" / "outbox-schema.sql"
GRANTS_SQL = PIPELINE_DIR / "sql" / "least-privilege-grants.sql"
ACL_RULES = PIPELINE_DIR / "redis-events" / "acl-rules.txt"

STREAM = "zetty:security-events"
DLQ = "zetty:security-events:dlq"
GROUP = "indexer"
INDEX_PATTERN = "zetty-security-events-v2-*"

MYSQL_USERS = {"zetty_relay": "mysql_relay", "zetty_indexer": "mysql_indexer", "zetty_pipeline_ops": "mysql_ops"}
REDIS_USERS = {"zetty-relay": "redis_relay", "zetty-indexer": "redis_indexer"}


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError("%s is not set; run pipeline/tests/integration/run.sh" % name)
    return value


def _hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def parse_utc(value: str) -> datetime:
    return datetime.strptime(
        value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z" if "." in value else "%Y-%m-%dT%H:%M:%S%z"
    )


def wait_until(predicate: Callable[[], bool], timeout_s: float = 60.0, interval_s: float = 0.5, what: str = "") -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return
        if time.monotonic() > deadline:
            raise AssertionError("timeout waiting for %s" % (what or predicate))
        time.sleep(interval_s)


@dataclass
class Counts:
    es_docs: int
    es_unique_ids: int
    receipts: int
    outbox_pending: int
    outbox_published: int
    stream_len: int
    pending: int
    dlq_len: int

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class Lab:
    run_id: str
    network: str
    secrets_dir: Path
    relay_image: str
    indexer_image: str
    mysql_container: str
    redis_container: str
    es_container: str
    mysql: Any = None
    admin_redis: redis.Redis | None = None
    es_url: str = ""
    started: list[str] = field(default_factory=list)
    report: list[dict[str, Any]] = field(default_factory=list)
    log_checked: int = 0
    leaked_logs: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ setup

    @classmethod
    def from_env(cls) -> Lab:
        lab = cls(
            run_id=_env("P02_RUN_ID"),
            network=_env("P02_NET"),
            secrets_dir=Path(_env("P02_SECRETS")),
            relay_image=_env("P02_RELAY_IMAGE"),
            indexer_image=_env("P02_INDEXER_IMAGE"),
            mysql_container=_env("P02_MYSQL_CONTAINER"),
            redis_container=_env("P02_REDIS_CONTAINER"),
            es_container=_env("P02_ES_CONTAINER"),
        )
        mysql_host, mysql_port = _hostport(_env("P02_MYSQL_HOSTPORT"))
        wait_until(lambda: lab._try_connect_mysql(mysql_host, mysql_port), 180, 1, "mysql")
        redis_host, redis_port = _hostport(_env("P02_REDIS_HOSTPORT"))
        lab.admin_redis = redis.Redis(
            host=redis_host, port=redis_port, password=lab.secret("redis_admin"), decode_responses=True
        )
        wait_until(lambda: lab._ping_redis(), 60, 0.5, "redis")
        lab.es_url = "http://" + _env("P02_ES_HOSTPORT")
        wait_until(lab._es_ready, 240, 2, "elasticsearch")
        lab._init_mysql()
        lab._init_redis_acl()
        return lab

    def secret(self, name: str) -> str:
        return (self.secrets_dir / name).read_text(encoding="utf-8").strip()

    def _try_connect_mysql(self, host: str, port: int) -> bool:
        try:
            self.mysql = pymysql.connect(
                host=host,
                port=port,
                user="root",
                password=self.secret("mysql_root"),
                database="zeti_db",
                autocommit=True,
                init_command="SET SESSION time_zone = '+00:00'",
            )
            return True
        except pymysql.err.OperationalError:
            return False

    def _ping_redis(self) -> bool:
        try:
            return bool(self.admin_redis.ping())
        except redis.ConnectionError:
            return False

    def _es_ready(self) -> bool:
        try:
            r = requests.get(self.es_url + "/_cluster/health", params={"wait_for_status": "yellow", "timeout": "5s"})
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _init_mysql(self) -> None:
        for stmt in _split_sql(SCHEMA_SQL.read_text(encoding="utf-8")):
            self.sql(stmt)
        for user, secret_name in MYSQL_USERS.items():
            self.sql("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (user, self.secret(secret_name)))
        for stmt in _split_sql(GRANTS_SQL.read_text(encoding="utf-8")):
            self.sql(stmt)

    def _init_redis_acl(self) -> None:
        for line in ACL_RULES.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            user, _, rules = line.partition(":")
            tokens = re.findall(r"\([^)]*\)|\S+", rules)
            self.admin_redis.execute_command(
                "ACL", "SETUSER", user.strip(), "reset", "on", ">" + self.secret(REDIS_USERS[user.strip()]), *tokens
            )

    # ------------------------------------------------------------ state

    def sql(self, statement: str, params: Any = None) -> list[tuple]:
        with self.mysql.cursor() as cur:
            cur.execute(statement, params)
            return list(cur.fetchall())

    def reset(self) -> None:
        self.sql("TRUNCATE TABLE security_event_outbox")
        self.sql("TRUNCATE TABLE security_event_receipt")
        self.admin_redis.flushall()
        requests.delete(self.es_url + "/" + INDEX_PATTERN, params={"expand_wildcards": "all"}).raise_for_status()

    def insert_events(self, docs: list[dict[str, Any]], occurred_at: list[datetime] | None = None) -> None:
        rows = []
        for i, doc in enumerate(docs):
            when = occurred_at[i] if occurred_at else parse_utc(doc["occurred_at"])
            rows.append(
                (
                    doc["event_id"],
                    doc.get("producer", "api"),
                    doc.get("event_type", "ACCESS_DECISION"),
                    when.astimezone(timezone.utc).replace(tzinfo=None),
                    json.dumps(doc),
                )
            )
        with self.mysql.cursor() as cur:
            cur.executemany(
                "INSERT INTO security_event_outbox (event_id, producer, event_type, occurred_at, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
            )

    def es(self, method: str, path: str, **kwargs) -> requests.Response:
        return requests.request(method, self.es_url + path, timeout=30, **kwargs)

    def es_refresh(self) -> None:
        self.es(
            "POST", "/" + INDEX_PATTERN + "/_refresh", params={"expand_wildcards": "open", "ignore_unavailable": "true"}
        )

    def es_docs(self) -> list[dict[str, Any]]:
        self.es_refresh()
        r = self.es(
            "GET",
            "/" + INDEX_PATTERN + "/_search",
            params={"expand_wildcards": "open", "ignore_unavailable": "true", "version": "true"},
            json={"size": 10000, "query": {"match_all": {}}},
        )
        r.raise_for_status()
        return r.json()["hits"]["hits"]

    def counts(self) -> Counts:
        hits = self.es_docs()
        (receipts,) = self.sql("SELECT COUNT(*) FROM security_event_receipt")[0]
        pending_rows, published_rows = self.sql(
            "SELECT COALESCE(SUM(status='PENDING'),0), COALESCE(SUM(status='PUBLISHED'),0) FROM security_event_outbox"
        )[0]
        return Counts(
            es_docs=len(hits),
            es_unique_ids=len({h["_id"] for h in hits}),
            receipts=int(receipts),
            outbox_pending=int(pending_rows),
            outbox_published=int(published_rows),
            stream_len=int(self.admin_redis.xlen(STREAM)),
            pending=self.pending(),
            dlq_len=int(self.admin_redis.xlen(DLQ)),
        )

    def pending(self) -> int:
        try:
            info = self.admin_redis.xpending(STREAM, GROUP)
        except redis.ResponseError:
            return 0
        return int(info["pending"])

    def pending_event_ids(self) -> set[str]:
        out = set()
        for item in self.admin_redis.xpending_range(STREAM, GROUP, min="-", max="+", count=10000):
            entries = self.admin_redis.xrange(STREAM, min=item["message_id"], max=item["message_id"])
            out.update(fields["event_id"] for _sid, fields in entries)
        return out

    def receipts(self) -> int:
        return int(self.sql("SELECT COUNT(*) FROM security_event_receipt")[0][0])

    def record(self, scenario: str, expected: dict[str, Any], actual: dict[str, Any], note: str = "") -> None:
        entry = {"scenario": scenario, "expected": expected, "actual": actual}
        if note:
            entry["note"] = note
        self.report.append(entry)
        print("SCENARIO %s expected=%s actual=%s %s" % (scenario, expected, actual, note))

    # ------------------------------------------------------------ containers

    def _common_env(self, mysql_user: str, mysql_secret: str) -> dict[str, str]:
        return {
            "MYSQL_HOST": self.mysql_container,
            "MYSQL_DATABASE": "zeti_db",
            "MYSQL_USER": mysql_user,
            "MYSQL_PASSWORD_FILE": "/run/p02/" + mysql_secret,
            "REDIS_EVENTS_HOST": self.redis_container,
            "LOG_LEVEL": "INFO",
        }

    def _run(self, name: str, image: str, env: dict[str, str], hostname: str | None = None, args=()) -> str:
        full = "%s-%s" % (self.run_id, name)
        cmd = ["docker", "run", "-d", "--name", full, "--network", self.network, "--memory", "256m", "--cpus", "1"]
        cmd += ["-v", "%s:/run/p02:ro" % self.secrets_dir]
        if hostname:
            cmd += ["--hostname", hostname]
        for key, value in env.items():
            cmd += ["-e", "%s=%s" % (key, value)]
        cmd += [image, *args]
        subprocess.run(cmd, check=True, capture_output=True)
        self.started.append(full)
        return full

    def run_relay(self, name: str, **overrides: str) -> str:
        env = self._common_env("zetty_relay", "mysql_relay")
        env.update(
            {
                "REDIS_EVENTS_USERNAME": "zetty-relay",
                "REDIS_EVENTS_PASSWORD_FILE": "/run/p02/redis_relay",
                "RELAY_BATCH_SIZE": "50",
                "RELAY_POLL_INTERVAL_MS": "200",
                "RELAY_LEASE_SECONDS": "30",
                "RELAY_STATS_INTERVAL_MS": "1000",
            }
        )
        env.update(overrides)
        return self._run(name, self.relay_image, env)

    def run_indexer(self, name: str, hostname: str | None = None, **overrides: str) -> str:
        env = self._common_env("zetty_indexer", "mysql_indexer")
        env.update(
            {
                "REDIS_EVENTS_USERNAME": "zetty-indexer",
                "REDIS_EVENTS_PASSWORD_FILE": "/run/p02/redis_indexer",
                "ES_URL": "http://%s:9200" % self.es_container,
                "INDEXER_BATCH_SIZE": "50",
                "INDEXER_BLOCK_MS": "500",
                "INDEXER_CLAIM_IDLE_MS": "3000",
                "INDEXER_CLAIM_INTERVAL_MS": "1000",
            }
        )
        env.update(overrides)
        return self._run(name, self.indexer_image, env, hostname=hostname or name)

    def run_recovery(self, *args: str) -> tuple[int, str]:
        env = self._common_env("zetty_pipeline_ops", "mysql_ops")
        cmd = ["docker", "run", "--rm", "--network", self.network, "-v", "%s:/run/p02:ro" % self.secrets_dir]
        for key, value in env.items():
            cmd += ["-e", "%s=%s" % (key, value)]
        cmd += ["--entrypoint", "python", self.relay_image, "-m", "pipeline.recovery", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, proc.stdout

    def stop(self, container: str, timeout_s: int = 10) -> None:
        subprocess.run(["docker", "stop", "-t", str(timeout_s), container], check=True, capture_output=True)

    def start(self, container: str) -> None:
        subprocess.run(["docker", "start", container], check=True, capture_output=True)

    def wait_exit(self, container: str, timeout_s: float = 60) -> int:
        def exited() -> bool:
            out = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", container], capture_output=True, text=True
            ).stdout.strip()
            return out == "exited"

        wait_until(exited, timeout_s, 0.5, "%s exit" % container)
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", container], capture_output=True, text=True
        )
        return int(out.stdout.strip())

    def metrics(self, container: str, port: int) -> dict[str, float]:
        code = (
            "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:%d/metrics',timeout=3).read().decode())"
            % port
        )
        out = subprocess.run(["docker", "exec", container, "python", "-c", code], capture_output=True, text=True)
        values: dict[str, float] = {}
        for line in out.stdout.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            key, _, value = line.rpartition(" ")
            try:
                values[key] = float(value)
            except ValueError:
                continue
        return values

    def logs(self, container: str) -> str:
        return subprocess.run(["docker", "logs", container], capture_output=True, text=True).stdout

    def secrets_in_logs(self) -> tuple[int, list[str]]:
        """시작한 컨테이너 로그에 이번 실행의 비밀번호가 나타나는지 검사한다(값은 반환하지 않음)."""
        values = [p.read_text(encoding="utf-8").strip() for p in self.secrets_dir.iterdir() if p.is_file()]
        values = [v for v in values if len(v) >= 16 and "\n" not in v]
        leaked = []
        for name in self.started:
            proc = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            text = proc.stdout + proc.stderr
            if any(v in text for v in values):
                leaked.append(name)
        return len(self.started), leaked

    def remove_started(self) -> None:
        for name in self.started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        self.log_checked += len(self.started)
        self.started.clear()

    def pause(self, container: str) -> None:
        subprocess.run(["docker", "pause", container], check=True, capture_output=True)

    def unpause(self, container: str) -> None:
        subprocess.run(["docker", "unpause", container], check=True, capture_output=True)


def _split_sql(text: str) -> list[str]:
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def new_id() -> str:
    return str(uuid.uuid4())
