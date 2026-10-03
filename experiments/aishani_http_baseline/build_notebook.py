"""Build the self-contained Colab notebook from the reviewed local source."""
import ast
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def markdown(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)}


def code(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": text.splitlines(keepends=True)}


SETUP = '''from pathlib import Path
import subprocess
import sys

ROOT = Path("/content/aishani_http_baseline")
ROOT.mkdir(parents=True, exist_ok=True)
requirements = """__REQUIREMENTS__"""
(ROOT / "requirements.txt").write_text(requirements)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                str(ROOT / "requirements.txt")], check=True)
print("Dependencies installed. Use a fresh CPU runtime; restart if versions were already imported.")
'''.replace("__REQUIREMENTS__", (HERE / "requirements.txt").read_text())

DOWNLOAD = '''import hashlib
import urllib.request

COMMIT = "e8c5fc3fd149985ef73a59df3f044368d607e5d6"
BASE = f"https://raw.githubusercontent.com/Aishani2001/VMWare-API-Misuse-or-Abuse/{COMMIT}/"
FILES = {
    "access2.log": "f15c31e905f86c7b4b6ab44aee74d0a2086dce89f010187d983edea7ef0364ef",
    "finaldata.csv": "3caacb4eddff809ecb5d9df606431534441f191e8287f831274074089725f793",
}
for name, expected in FILES.items():
    with urllib.request.urlopen(BASE + name, timeout=60) as response:
        data = response.read(5_000_001)
    if len(data) > 5_000_000 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Public reference checksum/size mismatch: " + name)
    (ROOT / name).write_bytes(data)
    print(name, len(data), "bytes; checksum verified")
# Do not display raw IPs, URLs, user agents or request records.
'''

TEST = '''test = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", str(ROOT), "-v"],
    capture_output=True, text=True,
)
print(test.stdout)
print(test.stderr)
test.check_returncode()
'''

BENCHMARK = '''import importlib.metadata
import json
import os
from datetime import datetime, timezone

expected = {line.split("==")[0]: line.split("==")[1]
            for line in requirements.splitlines() if "==" in line}
for package, version in expected.items():
    if importlib.metadata.version(package) != version:
        raise RuntimeError("Dependency version mismatch: " + package)

run_root = ROOT / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
reports = []
child_env = dict(os.environ, LOKY_MAX_CPU_COUNT="2")
for repetition in range(1, 4):
    output = run_root / f"run-{repetition}"
    result = subprocess.run([
        sys.executable, str(ROOT / "baseline.py"),
        "--log", str(ROOT / "access2.log"),
        "--reference-csv", str(ROOT / "finaldata.csv"),
        "--output", str(output), "--threads", "2",
    ], capture_output=True, text=True, env=child_env)
    if result.returncode:
        print(result.stderr)
        result.check_returncode()
    report = json.loads((output / "report.json").read_text())
    reports.append(report)
    print(f"Run {repetition}: golden={report['golden_check']['passed']}, "
          f"{report['timing']['pipeline_seconds_excluding_export']:.3f}s pipeline, "
          f"{report['memory']['process_lifetime_peak_rss_bytes'] / 2**20:.1f} MiB peak RSS")
'''

SUMMARY = '''import statistics

# Compare deterministic outcomes, then summarize timing repeats (not accuracy repeats).
for report in reports[1:]:
    for name in reports[0]["models"]:
        if report["models"][name]["flagged_subjects"] != reports[0]["models"][name]["flagged_subjects"]:
            raise RuntimeError("Flag count changed across repeats: " + name)
summary = {
    "environment_label": "Colab CPU; confirm actual environment below",
    "environment": reports[0]["environment"],
    "mode": reports[0]["mode"], "repetitions": len(reports), "seed": 42,
    "input_sha256": reports[0]["input_sha256"], "input_bytes": reports[0]["input_bytes"],
    "code_sha256": reports[0]["code_sha256"], "quality": reports[0]["quality"],
    "subjects": reports[0]["subjects"], "golden_check": reports[0]["golden_check"],
    "blas_openmp_thread_limit": reports[0]["blas_openmp_thread_limit"],
    "models": {}, "detection_metrics": reports[0]["detection_metrics"],
    "pipeline_seconds_median": statistics.median(r["timing"]["pipeline_seconds_excluding_export"] for r in reports),
    "end_to_end_seconds_excluding_report_write_median": statistics.median(r["timing"]["end_to_end_seconds_excluding_report_write"] for r in reports),
    "process_lifetime_peak_rss_bytes_max": max(r["memory"]["process_lifetime_peak_rss_bytes"] for r in reports),
}
for name, model in reports[0]["models"].items():
    summary["models"][name] = {
        "flagged_subjects": model["flagged_subjects"], "flag_rate": model["flag_rate"],
        "fit_seconds_median": statistics.median(r["models"][name]["fit_seconds"] for r in reports),
        "score_and_predict_seconds_median": statistics.median(r["models"][name]["score_and_predict_seconds"] for r in reports),
    }
(run_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\\n")
print("Environment:", summary["environment"])
print("Model      Flagged IPs   Fit (median s)   Score + predict (median s)")
for name, model in summary["models"].items():
    print(f"{name:10} {model['flagged_subjects']:11} {model['fit_seconds_median']:16.4f} {model['score_and_predict_seconds_median']:26.4f}")
print("No ground truth: precision/recall/F1/PR-AUC/FPR are unavailable.")
print("3 runs use seed=42. These repeats measure runtime, not accuracy uncertainty.")
'''

DOWNLOAD_RESULTS = '''import shutil
from google.colab import files

zip_path = shutil.make_archive(str(run_root) + "-results", "zip", root_dir=run_root)
files.download(zip_path)
# Contains report.json, pseudonymous predictions.csv and baseline.joblib for each run.
# Public raw access log is deliberately excluded. Save results before runtime ends.
'''


def build():
    cells = [
        markdown("""# Aishani HTTP 이상 탐지: 원본 방식 재현과 측정

**CPU 런타임 / 하드웨어 가속기 없음**으로 전체 실행하세요. GPU/TPU가 필요 없습니다.
실용 최소 추정은 CPU 2 cores / RAM 4 GB, 권장은 RAM 8 GB입니다. 원본 2.4 MB 자료 기준이며 Colab 자원은 고정 보장이 아닙니다.

이 노트북은 IP 전체 기간의 7개 피처, 전체 자료 MinMaxScaler, IF·OCSVM·KMeans의 원본 설정을 유지합니다.
원본 로그에는 공격 label이 없어 **탐지 정확도를 측정할 수 없습니다**. 모델이 이상으로 표시한 수와 실행 시간·프로세스 peak RSS를 측정합니다.
같은 자료를 학습·판정하는 원본 실험이며, 개선 모델의 holdout 성능과 직접 비교하지 않습니다.

[원본 고정 commit](https://github.com/Aishani2001/VMWare-API-Misuse-or-Abuse/tree/e8c5fc3fd149985ef73a59df3f044368d607e5d6)
· [Colab 자원 안내](https://research.google.com/colaboratory/faq.html)

공개 자료만 사용합니다. 인증정보·실사용자의 로그를 업로드하지 마세요. Drive 연결 없이 실행됩니다.
"""),
        markdown("## 1. 고정 의존성 설치\n새 런타임에서 시작하세요. 이미 로드한 numpy 등과 설치 버전이 다르면 설치 후 런타임을 다시 시작하세요."),
        code(SETUP),
        markdown("## 2. 독립 구현과 검증 코드\n원본 소스 복사가 아닌 알고리즘 재현입니다. 첫 간격 0·active-day 평균·누락 바이트 0·ddof=1을 유지합니다. KMeans n_init=10을 명시했습니다. elbow/t-SNE 그래프는 생략했습니다."),
        code("%%writefile /content/aishani_http_baseline/baseline.py\n" + (HERE / "baseline.py").read_text()),
        code("%%writefile /content/aishani_http_baseline/test_baseline.py\n" + (HERE / "test_baseline.py").read_text()),
        markdown("## 3. 고정 공개 자료 다운로드\ncommit과 SHA-256을 확인합니다. 원문 로그는 출력하지 않습니다."),
        code(DOWNLOAD),
        markdown("## 4. 독립 fixture 검증"),
        code(TEST),
        markdown("## 5. 원본 CSV 대조 후 새 프로세스에서 3회 측정\n1,753개 IP × 7개 값 = 12,271개 피처 값을 원본 CSV와 대조합니다. seed=42로 고정합니다. 모델 fit/predict 시간은 프로세스 import와 설치·다운로드 시간을 제외합니다."),
        code(BENCHMARK),
        markdown("## 6. 측정 요약\nflag rate는 오탐률(FPR)이 아닙니다. peak RSS는 import를 포함한 하위 프로세스 생애 최대 메모리이며 notebook 전체 메모리가 아닙니다."),
        code(SUMMARY),
        markdown("## 7. 결과 저장\nZIP을 로컬에 저장하세요. 향후 같은 label 자료와 평가 단위, split을 고정한 뒤 원본 방식의 heldout 평가와 개선 모델을 비교합니다. 모델은 신뢰한 자기 산출물과 checksum만 확인해서 로드하세요."),
        code(DOWNLOAD_RESULTS),
    ]
    for cell in cells:
        if cell["cell_type"] == "code":
            source = "".join(cell["source"])
            if source.startswith("%%writefile "):
                source = source.split("\n", 1)[1]
            ast.parse(source)
    notebook = {"cells": cells, "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
        "colab": {"name": "aishani_http_baseline.ipynb", "provenance": []},
    }, "nbformat": 4, "nbformat_minor": 4}
    target = ROOT / "notebooks" / "aishani_http_baseline.ipynb"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(notebook, ensure_ascii=False, indent=2) + "\n")
    print(f"Built {target.name}: {len(cells)} cells; all code cells syntax-checked")


if __name__ == "__main__":
    build()
