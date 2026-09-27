"""프로세스 운영 보조: 로그, 종료 신호, backoff, health, /metrics HTTP, 장애 주입."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

LOG = logging.getLogger("pipeline")

# 시험 전용 장애 주입 지점. 기본값(빈 값)에서는 아무 동작도 하지 않는다.
FAILPOINT_ENV = "PIPELINE_FAILPOINT"
FAILPOINT_EXIT_CODE = 86


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def error_label(exc: BaseException) -> str:
    """예외는 클래스 이름과 (있으면) 숫자 코드만 남긴다. 메시지에는 SQL·값이 섞일 수 있다."""
    code = ""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        code = ":%d" % args[0]
    return type(exc).__name__ + code


def install_stop_handlers() -> threading.Event:
    stop = threading.Event()

    def _handler(signum, _frame):
        LOG.info("stop signal received signum=%d", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    return stop


class Backoff:
    def __init__(self, initial_s: float = 0.5, max_s: float = 30.0) -> None:
        self._initial = initial_s
        self._max = max_s
        self._next = initial_s

    def next(self) -> float:
        value = self._next
        self._next = min(self._next * 2, self._max)
        return value

    def reset(self) -> None:
        self._next = self._initial


class Health:
    """heartbeat: 루프가 돌고 있는가(liveness). success: 마지막 성공이 최근인가(readiness)."""

    def __init__(self, stale_after_s: float, clock=time.monotonic) -> None:
        self._stale = stale_after_s
        self._clock = clock
        self._beat = clock()
        self._success: float | None = None

    def beat(self) -> None:
        self._beat = self._clock()

    def success(self) -> None:
        now = self._clock()
        self._beat = now
        self._success = now

    def alive(self) -> bool:
        return self._clock() - self._beat <= self._stale

    def ready(self) -> bool:
        return self._success is not None and self._clock() - self._success <= self._stale


def start_http_server(host: str, port: int, registry: CollectorRegistry, health: Health) -> ThreadingHTTPServer:
    """GET /metrics(Prometheus text), /healthz(liveness), /readyz(readiness)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 규약
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                self._send(200, generate_latest(registry), CONTENT_TYPE_LATEST)
            elif path == "/healthz":
                ok = health.alive()
                self._send(200 if ok else 503, b"ok\n" if ok else b"stale\n", "text/plain")
            elif path == "/readyz":
                ok = health.ready()
                self._send(200 if ok else 503, b"ready\n" if ok else b"not ready\n", "text/plain")
            else:
                self._send(404, b"not found\n", "text/plain")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - 접근 로그를 남기지 않는다
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
    thread.start()
    return server


def failpoint(name: str) -> None:
    """`PIPELINE_FAILPOINT=name`이면 정리 없이 즉시 종료한다(ACK 전 crash 재현용)."""
    if os.environ.get(FAILPOINT_ENV) == name:
        LOG.warning("failpoint hit name=%s; exiting without cleanup", name)
        logging.shutdown()
        os._exit(FAILPOINT_EXIT_CODE)
