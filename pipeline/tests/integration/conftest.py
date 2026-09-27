from __future__ import annotations

import json
import os

import pytest

from pipeline.tests.integration.lab import Lab

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def lab():
    if not os.environ.get("P02_RUN_ID"):
        pytest.skip("통합 시험은 pipeline/tests/integration/run.sh 로 실행한다")
    instance = Lab.from_env()
    yield instance
    instance.remove_started()
    instance.report.append(
        {
            "scenario": "no_secrets_in_container_logs",
            "expected": {"containers_with_secret_in_logs": 0},
            "actual": {
                "containers_checked": instance.log_checked,
                "containers_with_secret_in_logs": len(instance.leaked_logs),
            },
        }
    )
    report_path = os.environ.get("P02_REPORT")
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(instance.report, fh, ensure_ascii=False, indent=2)


@pytest.fixture()
def fresh(lab):
    lab.reset()
    yield lab
    _checked, leaked = lab.secrets_in_logs()
    lab.leaked_logs.extend(leaked)
    lab.remove_started()
    assert not leaked, "credential value found in container logs: %s" % leaked
