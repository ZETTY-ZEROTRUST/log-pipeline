"""Strict actor-window dataset contract; labels and identifiers never enter X."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

import numpy as np

FEATURES = (
    "request_count", "mean_interval_s", "interval_std_s", "distinct_uri_count",
    "response_bytes_sum", "response_bytes_mean", "response_bytes_std",
    "4xx_ratio", "5xx_ratio", "GET_ratio", "POST_ratio", "burst_max_per_s",
)
EXTRA = ("GET_count", "successful_2xx_count")
LABEL_FIELDS = {
    "window_id", "label", "split", "profile", "cohort_id",
    "window_start_utc", "window_end_utc",
}
REFERENCE_FEATURES = (
    "request_count", "active_window_mean_count", "GET_count", "successful_2xx_count",
    "response_bytes_mean", "response_bytes_std", "mean_interval_s",
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def csv_rows(path: Path, expected: set[str]) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or set(fields) != expected:
            raise ValueError(f"{path.name}: column contract mismatch; values omitted")
        rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError(f"{path.name}: empty or malformed records")
    return rows


def utc_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("Dataset timestamps must use UTC")
    return stamp


@dataclass(frozen=True)
class Dataset:
    window_ids: tuple[str, ...]
    improved: np.ndarray
    reference: np.ndarray
    labels: np.ndarray
    splits: np.ndarray
    profiles: tuple[str, ...]
    counts: dict
    hashes: dict
    metadata: dict

    def mask(self, split: str) -> np.ndarray:
        return self.splits == split


def load_dataset(root: Path) -> Dataset:
    features_path, labels_path = root / "features.csv", root / "labels.csv"
    features = csv_rows(features_path, {"window_id", *FEATURES, *EXTRA})
    labels = csv_rows(labels_path, LABEL_FIELDS)
    feature_by_id = {row["window_id"]: row for row in features}
    label_by_id = {row["window_id"]: row for row in labels}
    if len(feature_by_id) != len(features) or len(label_by_id) != len(labels):
        raise ValueError("Duplicate window ID")
    if not feature_by_id or set(feature_by_id) != set(label_by_id):
        raise ValueError("Feature/label coverage differs")
    improved, reference, truth, split_names, profiles, times = [], [], [], [], [], {}
    ordered = sorted(feature_by_id)
    for wid in ordered:
        feature, label = feature_by_id[wid], label_by_id[wid]
        if label["label"] not in ("0", "1"):
            raise ValueError("Binary labels must be 0 or 1")
        split = label["split"]
        if split not in ("train", "calibration", "test"):
            raise ValueError("Unknown split")
        start, end = utc_time(label["window_start_utc"]), utc_time(label["window_end_utc"])
        if (end - start).total_seconds() != 60:
            raise ValueError("Each observation must represent one full 60-second window")
        times.setdefault(split, []).append((start, end))
        row = [float(feature[name]) for name in (*FEATURES, *EXTRA)]
        if not np.isfinite(row).all() or min(row) < 0:
            raise ValueError("Non-finite or negative features")
        values = dict(zip((*FEATURES, *EXTRA), row))
        n = values["request_count"]
        if n < 1 or n != int(n):
            raise ValueError("request_count must be a positive integer")
        for name in ("GET_count", "successful_2xx_count", "distinct_uri_count", "burst_max_per_s"):
            if values[name] != int(values[name]) or values[name] > n:
                raise ValueError("Count feature outside request_count")
        for name in ("4xx_ratio", "5xx_ratio", "GET_ratio", "POST_ratio"):
            if values[name] > 1:
                raise ValueError("Invalid ratio")
        if values["4xx_ratio"] + values["5xx_ratio"] > 1 + 1e-9:
            raise ValueError("Status ratios exceed one")
        if not np.isclose(values["GET_ratio"] * n, values["GET_count"], atol=1e-8):
            raise ValueError("GET count/ratio mismatch")
        if not np.isclose(values["response_bytes_mean"] * n, values["response_bytes_sum"], rtol=1e-8):
            raise ValueError("Bytes mean/sum mismatch")
        improved.append(row[:len(FEATURES)])
        # Original Daily Mean is total / active days. A 60s window has one active day,
        # so this adaptation intentionally preserves the duplicate count feature.
        reference.append([n, n, values["GET_count"], values["successful_2xx_count"],
                          values["response_bytes_mean"], values["response_bytes_std"],
                          values["mean_interval_s"]])
        truth.append(int(label["label"]))
        split_names.append(split)
        profiles.append(label["profile"])
    if set(times) != {"train", "calibration", "test"}:
        raise ValueError("All three splits are required")
    for earlier, later in (("train", "calibration"), ("calibration", "test")):
        if max(end for _, end in times[earlier]) > min(start for start, _ in times[later]):
            raise ValueError("Splits overlap or are not chronological")
    y, splits = np.asarray(truth, dtype=np.int64), np.asarray(split_names)
    counts = {}
    for split in ("train", "calibration", "test"):
        selected = y[splits == split]
        counts[split] = {"windows": len(selected), "normal": int((selected == 0).sum()),
                         "attack": int((selected == 1).sum())}
    if counts["train"]["attack"] or counts["calibration"]["attack"]:
        raise ValueError("Training and false-positive calibration must contain only normal windows")
    if counts["train"]["normal"] < 10 or counts["calibration"]["normal"] < 10:
        raise ValueError("At least 10 normal train and calibration windows required")
    if not counts["test"]["normal"] or not counts["test"]["attack"]:
        raise ValueError("Holdout requires normal and attack windows")
    metadata = {}
    for name in ("manifest.json", "quality.json"):
        path = root / name
        if not path.is_file():
            raise ValueError("Dataset manifest and quality gate are required")
        metadata[name] = json.loads(path.read_text(encoding="utf-8"))
    quality = metadata["quality.json"]
    if quality.get("pass") is not True or quality.get("status") != "pass":
        raise ValueError("Dataset quality gate failed")
    if metadata["manifest.json"].get("schema_version") != "zetty.s4-http/1":
        raise ValueError("Unknown dataset manifest schema")
    hashes = {name: file_hash(root / name) for name in
              ("features.csv", "labels.csv", "manifest.json", "quality.json") if (root / name).exists()}
    return Dataset(tuple(ordered), np.asarray(improved), np.asarray(reference),
                   y, splits, tuple(profiles), counts, hashes, metadata)
