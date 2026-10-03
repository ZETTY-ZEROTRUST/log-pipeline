"""Independent reproduction of Aishani's unlabeled, in-sample HTTP experiment.

No service connections or network calls. Only public/sanitized Apache access logs.
The historical aggregation choices are deliberately preserved for comparison.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import resource
import statistics
import sys
import time

import joblib
import numpy as np
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import MinMaxScaler
from sklearn.svm import OneClassSVM
from threadpoolctl import threadpool_limits

REFERENCE_COMMIT = "e8c5fc3fd149985ef73a59df3f044368d607e5d6"
REFERENCE_URL = "https://github.com/Aishani2001/VMWare-API-Misuse-or-Abuse"
DATA_SHA256 = "f15c31e905f86c7b4b6ab44aee74d0a2086dce89f010187d983edea7ef0364ef"
GOLDEN_SHA256 = "3caacb4eddff809ecb5d9df606431534441f191e8287f831274074089725f793"
VERSION = "aishani-http-reference/1"
FEATURES = (
    "Total Requests", "Daily Mean", "GET requests", "Successful requests",
    "Mean Return Size", "Return Size Std", "Mean Time Difference",
)
# Preserve the original IPv4-like matching and silently skipped non-matches.
LOG_PATTERN = re.compile(r'([\d\.]+) - - \[(.*?)\] "(.*?)" (\d+) (.+) "(.*?)" "(.*?)"')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def subject_id(ip: str) -> str:
    """Stable public-data join key; this is not a privacy guarantee for real IPs."""
    return hashlib.sha256((VERSION + "\0" + ip).encode()).hexdigest()


def aggregate_lines(lines) -> tuple[list[str], np.ndarray, dict]:
    by_ip: dict[str, list[tuple]] = {}
    quality = {"lines": 0, "parsed": 0, "skipped": 0, "missing_bytes_mapped_to_zero": 0}
    for line in lines:
        quality["lines"] += 1
        match = LOG_PATTERN.match(line)
        if match is None:
            quality["skipped"] += 1
            continue
        ip, timestamp, request, status, size, _referer, _ua = match.groups()
        try:
            # Original notebook discards the numeric timezone offset.
            stamp = datetime.strptime(timestamp.split(" ")[0], "%d/%b/%Y:%H:%M:%S")
            missing = "-" in size
            returned = 0 if missing else int(size)
            success = int(200 <= int(status) <= 299)
        except ValueError:
            raise ValueError(f"Invalid matched record at line {quality['lines']}; raw values omitted") from None
        quality["missing_bytes_mapped_to_zero"] += int(missing)
        quality["parsed"] += 1
        by_ip.setdefault(ip, []).append((stamp, returned, int("GET" in request[:4]), success))
    if not by_ip:
        raise ValueError("No records matched the historical IPv4 access-log parser")
    ips = sorted(by_ip)
    rows = []
    for ip in ips:
        records = sorted(by_ip[ip], key=lambda item: item[0])
        day_counts: dict = {}
        gaps = []
        previous = None
        for stamp, _size, _get, _success in records:
            day = stamp.date()
            day_counts[day] = day_counts.get(day, 0) + 1
            gap = 0 if previous is None or previous.date() != day else (stamp - previous).seconds
            gaps.append(gap)
            previous = stamp
        sizes = [r[1] for r in records]
        rows.append([
            len(records), len(records) / len(day_counts),
            sum(r[2] for r in records), sum(r[3] for r in records),
            statistics.mean(sizes), statistics.stdev(sizes) if len(sizes) > 1 else 0,
            statistics.mean(gaps),
        ])
    return ips, np.asarray(rows, dtype=np.float64), quality


def check_reference(ips: list[str], matrix: np.ndarray, golden_path: Path) -> dict:
    """Compare every feature against the author's CSV, without emitting raw IPs."""
    if sha256_file(golden_path) != GOLDEN_SHA256:
        raise ValueError("Reference CSV checksum mismatch")
    with golden_path.open(newline="", encoding="utf-8") as source:
        expected = {row["IP"]: [float(row[name]) for name in FEATURES] for row in csv.DictReader(source)}
    if set(ips) != set(expected):
        raise ValueError("Reference subject set mismatch")
    target = np.asarray([expected[ip] for ip in ips])
    if not np.allclose(matrix, target, rtol=1e-10, atol=1e-9):
        raise ValueError("Feature reproduction differs from reference CSV")
    return {
        "passed": True, "subjects": len(ips), "feature_values": matrix.size,
        "max_absolute_difference": float(np.max(np.abs(matrix - target))),
        "rtol": 1e-10, "atol": 1e-9, "reference_csv_sha256": GOLDEN_SHA256,
    }


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def machine_info() -> dict:
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        ram = None
    return {
        "platform": platform.platform(), "machine": platform.machine(),
        "cpu_count": os.cpu_count(), "joblib_effective_cpu_count": joblib.cpu_count(),
        "loky_cpu_limit": os.environ.get("LOKY_MAX_CPU_COUNT"), "physical_ram_bytes": ram,
        "python": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name) for name in
                         ("numpy", "scikit-learn", "scipy", "joblib", "threadpoolctl")},
    }


def run(log_path: Path, output: Path, golden_path: Path | None = None,
        models: tuple[str, ...] = ("iforest", "ocsvm", "kmeans"), threads: int = 2) -> dict:
    if threads < 1 or not models or len(set(models)) != len(models):
        raise ValueError("Positive threads and distinct models are required")
    if any(name not in ("iforest", "ocsvm", "kmeans") for name in models):
        raise ValueError("Unknown model")
    # Never overwrite an earlier run or its model artifacts.
    if output.exists():
        raise FileExistsError("Run output already exists; use a new directory")
    started = time.perf_counter()
    data_hash = sha256_file(log_path)
    stage = time.perf_counter()
    with log_path.open(encoding="utf-8") as source:
        ips, matrix, quality = aggregate_lines(source)
    features_s = time.perf_counter() - stage
    golden = None
    if golden_path is not None:
        if data_hash != DATA_SHA256:
            raise ValueError("Golden comparison requires the pinned public reference log")
        golden = check_reference(ips, matrix, golden_path)
    scaler = MinMaxScaler()
    stage = time.perf_counter()
    scaled = scaler.fit_transform(matrix)
    scaling_s = time.perf_counter() - stage
    summaries = {}
    estimators = {}
    scores_by_model = {}
    flags_by_model = {}
    with threadpool_limits(limits=threads):
        for name in models:
            if name == "iforest":
                params = dict(n_jobs=-1, n_estimators=200, max_features=3,
                              random_state=42, contamination=0.01)
                estimator = IsolationForest(**params)
                fit_data = scaled
                threshold = 0.0
                score_definition = "negative decision_function; larger is more anomalous"
            elif name == "ocsvm":
                params = dict(nu=0.01, degree=2, kernel="rbf", gamma="scale")
                estimator = OneClassSVM(**params)
                cutoff = float(np.quantile(matrix[:, 0], .99))
                fit_data = scaled[matrix[:, 0] <= cutoff]
                threshold = 0.0
                score_definition = "negative decision_function; larger is more anomalous"
            else:
                if len(ips) < 5:
                    raise ValueError("KMeans reproduction requires at least five subjects")
                params = dict(n_clusters=5, random_state=42, n_init=10)
                estimator = KMeans(**params)
                fit_data = scaled
                threshold = 4.7
                score_definition = "sum of squared distances to ALL five centroids"
            stage = time.perf_counter()
            estimator.fit(fit_data)
            fit_s = time.perf_counter() - stage
            stage = time.perf_counter()
            if name == "kmeans":
                scores = np.square(estimator.transform(scaled)).sum(axis=1)
                flags = scores >= threshold
            else:
                scores = -estimator.decision_function(scaled)
                flags = estimator.predict(scaled) == -1
            predict_s = time.perf_counter() - stage
            scores_by_model[name] = scores
            flags_by_model[name] = flags
            estimators[name] = estimator
            summaries[name] = {
                "params": params, "training_subjects": len(fit_data),
                "scoring_subjects": len(ips), "flagged_subjects": int(flags.sum()),
                "flag_rate": float(flags.mean()), "threshold": threshold,
                "score_definition": score_definition, "fit_seconds": fit_s,
                "score_and_predict_seconds": predict_s,
                "score_quantiles": dict(zip(("min", "p50", "p95", "p99", "max"),
                                            np.quantile(scores, (0, .5, .95, .99, 1)).tolist())),
            }
            if name == "ocsvm":
                summaries[name]["training_request_count_p99"] = cutoff
    pipeline_s = time.perf_counter() - started
    output.mkdir(parents=True, exist_ok=False)
    header = ["subject_id", *FEATURES]
    for name in models:
        header.extend((name + "_score", name + "_flag"))
    with (output / "predictions.csv").open("w", newline="", encoding="utf-8") as destination:
        writer = csv.writer(destination)
        writer.writerow(header)
        for index, ip in enumerate(ips):
            row = [subject_id(ip), *matrix[index].tolist()]
            for name in models:
                row.extend((float(scores_by_model[name][index]), int(flags_by_model[name][index])))
            writer.writerow(row)
    artifact = output / "baseline.joblib"
    joblib.dump({"version": VERSION, "features": FEATURES, "scaler": scaler,
                 "estimators": estimators, "reference_commit": REFERENCE_COMMIT,
                 "input_sha256": data_hash, "mode": "reference_reproduction_in_sample",
                 "thresholds": {name: summaries[name]["threshold"] for name in models},
                 "score_definitions": {name: summaries[name]["score_definition"] for name in models}},
                artifact, compress=3)
    report = {
        "version": VERSION, "mode": "reference_reproduction_in_sample",
        "reference": {"url": REFERENCE_URL, "commit": REFERENCE_COMMIT},
        "code_sha256": sha256_file(Path(__file__)), "seed": 42,
        "input_sha256": data_hash, "input_bytes": log_path.stat().st_size,
        "unit": "one IP over the full input period", "subjects": len(ips),
        "features": list(FEATURES), "quality": quality, "golden_check": golden,
        "environment": machine_info(), "blas_openmp_thread_limit": threads,
        "isolation_forest_jobs": "all visible CPU cores (n_jobs=-1)",
        "models": summaries,
        "detection_metrics": {"precision": None, "recall": None, "f1": None,
                              "pr_auc": None, "fpr": None,
                              "reason": "No ground-truth attack labels; flag_rate is not FPR"},
        "timing": {"feature_seconds": features_s, "scaling_seconds": scaling_s,
                   "pipeline_seconds_excluding_export": pipeline_s,
                   "end_to_end_seconds_excluding_report_write": time.perf_counter() - started},
        "memory": {"process_lifetime_peak_rss_bytes": peak_rss_bytes(),
                   "scope": "current process including imports; not incremental dataset memory"},
        "artifacts": {path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
                      for path in (artifact, output / "predictions.csv")},
        "reproduction_limits": [
            "Original dependency versions were not locked; model outputs are version dependent",
            "KMeans historical default n_init=10 is explicit; modern default auto is not used",
            "Elbow sweep and t-SNE figures are omitted; they do not enter anomaly scoring",
            "Only algorithmic features are reproduced, not original dataframe/raw CSV memory use",
            "No held-out accuracy or token-theft detection claim follows from this experiment",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reference-csv", type=Path)
    parser.add_argument("--models", nargs="+", choices=("iforest", "ocsvm", "kmeans"),
                        default=("iforest", "ocsvm", "kmeans"))
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    report = run(args.log, args.output, args.reference_csv, tuple(args.models), args.threads)
    print(json.dumps({"mode": report["mode"], "subjects": report["subjects"],
                      "quality": report["quality"], "golden_check": report["golden_check"],
                      "models": report["models"], "timing": report["timing"],
                      "memory": report["memory"]}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
