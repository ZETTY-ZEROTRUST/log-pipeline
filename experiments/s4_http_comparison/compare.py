"""CPU-only, heldout S4 comparison. This module never connects to services."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import resource
import sys
import time

import joblib
import numpy as np
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import MinMaxScaler
from sklearn.svm import OneClassSVM
from threadpoolctl import threadpool_limits

from dataset import FEATURES, REFERENCE_FEATURES, file_hash, load_dataset

VERSION = "s4-http-comparison/1"
ARMS = ("reference_adapted", "reference_calibrated", "improved_behavior")
MODELS = ("iforest", "ocsvm", "kmeans")


def calibration_threshold(scores: np.ndarray, target_fpr: float) -> float:
    """Normal-only empirical upper quantile; strict > keeps boundary ties normal."""
    if not 0 < target_fpr < 1 or not len(scores) or not np.isfinite(scores).all():
        raise ValueError("Invalid calibration scores or target FPR")
    return float(np.quantile(scores, 1 - target_fpr, method="higher"))


def wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    spread = z * np.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [float(max(0, center - spread)), float(min(1, center + spread))]


def evaluate(y: np.ndarray, scores: np.ndarray, flags: np.ndarray) -> dict:
    tp = int(((y == 1) & flags).sum())
    fp = int(((y == 0) & flags).sum())
    tn = int(((y == 0) & ~flags).sum())
    fn = int(((y == 1) & ~flags).sum())
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    both = len(np.unique(y)) == 2
    return {
        "windows": len(y), "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "accuracy": (tp + tn) / len(y),
        "precision": precision, "recall": recall,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "fpr": fp / (fp + tn) if fp + tn else None,
        "pr_auc_average_precision": float(average_precision_score(y, scores)) if both else None,
        "roc_auc": float(roc_auc_score(y, scores)) if both else None,
        "precision_wilson_95": wilson(tp, tp + fp),
        "recall_wilson_95": wilson(tp, tp + fn),
        "fpr_wilson_95": wilson(fp, fp + tn),
        "interval_assumption": "Independent windows; repeated actors/time correlation can invalidate this assumption",
        "flagged": tp + fp,
    }


def estimator_for(name: str):
    if name == "iforest":
        return IsolationForest(n_estimators=200, max_features=3, contamination=.01,
                               random_state=42, n_jobs=1)
    if name == "ocsvm":
        return OneClassSVM(nu=.01, degree=2, kernel="rbf", gamma="scale")
    return KMeans(n_clusters=5, random_state=42, n_init=10)


def anomaly_score(estimator, name: str, matrix: np.ndarray, improved: bool) -> np.ndarray:
    if name != "kmeans":
        return -estimator.decision_function(matrix)
    distances = np.square(estimator.transform(matrix))
    return distances.min(axis=1) if improved else distances.sum(axis=1)


def run(root: Path, output: Path, target_fpr: float = .01, threads: int = 2) -> dict:
    if output.exists():
        raise FileExistsError("Output already exists; use a new run directory")
    if threads < 1 or not 0 < target_fpr < 1:
        raise ValueError("Positive threads and target FPR within (0,1) required")
    started = time.perf_counter()
    dataset = load_dataset(root)
    train, calibration, test = (dataset.mask(split) for split in ("train", "calibration", "test"))
    summaries, artifacts, predictions = {}, {}, []
    with threadpool_limits(limits=threads):
        for arm in ARMS:
            improved = arm == "improved_behavior"
            matrix = dataset.improved if improved else dataset.reference
            names = FEATURES if improved else REFERENCE_FEATURES
            scaler = MinMaxScaler().fit(matrix[train])
            scaled_train = scaler.transform(matrix[train])
            scaled_cal = scaler.transform(matrix[calibration])
            scaled_test = scaler.transform(matrix[test])
            summaries[arm] = {}
            for name in MODELS:
                fit_data = scaled_train
                cutoff = None
                if name == "ocsvm" and not improved:
                    cutoff = float(np.quantile(matrix[train, 0], .99))
                    fit_data = scaled_train[matrix[train, 0] <= cutoff]
                estimator = estimator_for(name)
                stage = time.perf_counter()
                estimator.fit(fit_data)
                fit_seconds = time.perf_counter() - stage
                stage = time.perf_counter()
                cal_scores = anomaly_score(estimator, name, scaled_cal, improved)
                if arm == "reference_adapted":
                    threshold = 4.7 if name == "kmeans" else 0.0
                    inclusive = name == "kmeans"
                    source = "Original fixed threshold, transported to actor-window adaptation"
                else:
                    threshold = calibration_threshold(cal_scores, target_fpr)
                    inclusive = False
                    source = "Normal-only calibration empirical upper quantile"
                # Test first enters the model here. No threshold/model selection uses test.
                test_scores = anomaly_score(estimator, name, scaled_test, improved)
                flags = test_scores >= threshold if inclusive else test_scores > threshold
                cal_flags = cal_scores >= threshold if inclusive else cal_scores > threshold
                scoring_seconds = time.perf_counter() - stage
                result = evaluate(dataset.labels[test], test_scores, flags)
                result.update({
                    "feature_names": list(names), "train_windows": int(len(fit_data)),
                    "params": estimator.get_params(), "training_request_count_p99": cutoff,
                    "threshold": threshold, "threshold_comparison": ">=" if inclusive else ">",
                    "threshold_source": source, "target_calibration_fpr": target_fpr if arm != ARMS[0] else None,
                    "observed_calibration_fpr": float(cal_flags.mean()),
                    "fit_seconds": fit_seconds, "calibration_and_test_scoring_seconds": scoring_seconds,
                    "score_definition": ("Nearest centroid squared distance" if improved else
                                         "Sum of squared distances to ALL five centroids")
                                        if name == "kmeans" else "Negative decision_function",
                })
                profiles = np.asarray(dataset.profiles)[test]
                result["test_by_profile"] = {
                    profile: evaluate(dataset.labels[test][profiles == profile],
                                      test_scores[profiles == profile], flags[profiles == profile])
                    for profile in sorted(set(profiles))
                }
                summaries[arm][name] = result
                artifacts[f"{arm}/{name}"] = {
                    "version": VERSION, "scaler": scaler, "estimator": estimator,
                    "feature_names": names, "threshold": threshold, "inclusive": inclusive,
                    "dataset_hashes": dataset.hashes,
                }
                for wid, label, score, flag in zip(np.asarray(dataset.window_ids)[test],
                                                    dataset.labels[test], test_scores, flags):
                    predictions.append([arm, name, wid, int(label), float(score), int(flag)])
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "schema_version": VERSION, "measurement_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_hashes": dataset.hashes, "split_counts": dataset.counts,
        "dataset_schema_version": dataset.metadata["manifest.json"]["schema_version"],
        "dataset_version": dataset.metadata["manifest.json"].get("dataset_version"),
        "dataset_quality_pass": dataset.metadata["quality.json"]["pass"],
        "code_hashes": {name: file_hash(Path(__file__).with_name(name))
                        for name in ("dataset.py", "compare.py")},
        "seed": 42, "window_seconds": 60, "threads": threads, "target_calibration_fpr": target_fpr,
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "dependencies": {name: importlib.metadata.version(name) for name in
                                         ("numpy", "scikit-learn", "scipy", "joblib", "threadpoolctl")}},
        "pipeline_seconds_before_artifact_write": time.perf_counter() - started,
        "process_lifetime_peak_rss_bytes": int(peak if sys.platform == "darwin" else peak * 1024),
        "models": summaries,
        "limitations": [
            "Local controlled S4 token-use simulation; not evidence of production token theft detection",
            "Reference adaptation changes IP/full-period aggregation to actor/60s-window and fits only train",
            "Calibration quantile targets 1% by default; observed holdout FPR is not guaranteed",
            f"Calibration normal windows={dataset.counts["calibration"]["normal"]}; small-tail estimates need more independent observations",
            "Repeated actor/windows are correlated; Wilson intervals only show binomial sampling uncertainty",
            f"Test attack windows={dataset.counts["test"]["attack"]}; recall resolution={1 / dataset.counts["test"]["attack"]:.4f}; pilot cannot establish superiority",
            "No model or threshold selection from test; all predeclared arms reported",
            "Response bytes are client-observed body bytes; not fabricated backend response_body_bytes",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with (output / "predictions.csv").open("w", newline="") as dest:
        writer = csv.writer(dest)
        writer.writerow(("arm", "model", "window_id", "label", "anomaly_score", "flag"))
        writer.writerows(predictions)
    # Created here and trusted locally; never load third-party pickle/joblib artifacts.
    joblib.dump(artifacts, output / "models.joblib")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-fpr", type=float, default=.01)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    report = run(args.dataset, args.output, args.target_fpr, args.threads)
    print(json.dumps({"output": str(args.output), "splits": report["split_counts"],
                      "models": {arm: {name: {key: values[key] for key in
                                             ("precision", "recall", "f1", "fpr", "confusion_matrix")}
                                       for name, values in models.items()}
                                 for arm, models in report["models"].items()}}, indent=2))


if __name__ == "__main__":
    main()
