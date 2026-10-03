"""Independent contracts, leakage guards, thresholds and metric fixtures."""
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from compare import calibration_threshold, evaluate, run
from dataset import EXTRA, FEATURES, LABEL_FIELDS, load_dataset


def write(path, fields, rows):
    with path.open("w", newline="") as dest:
        writer = csv.DictWriter(dest, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fixture(root):
    root.mkdir()
    features, labels = [], []
    base = datetime(2026, 10, 3, tzinfo=timezone.utc)
    for index, (split, size) in enumerate((("train", 20), ("calibration", 20), ("test", 10))):
        start = base + timedelta(minutes=index)
        for i in range(size):
            count = 20 + i + index * 1000
            values = [count, 1 / (i + 1), .2, 3, count * 100, 100, 10, 0, 0, 1, 0, 2, count, count]
            wid = f"{split}-{i}"
            features.append({"window_id": wid, **dict(zip((*FEATURES, *EXTRA), values))})
            labels.append({"window_id": wid, "label": int(split == "test" and i < 2),
                           "split": split, "profile": "fixture", "cohort_id": "fixture",
                           "window_start_utc": start.isoformat(),
                           "window_end_utc": (start + timedelta(seconds=60)).isoformat()})
    write(root / "features.csv", ("window_id", *FEATURES, *EXTRA), features)
    write(root / "labels.csv", sorted(LABEL_FIELDS), labels)
    (root / "quality.json").write_text(json.dumps({"pass": True, "status": "pass"}))
    (root / "manifest.json").write_text(json.dumps({"schema_version": "zetty.s4-http/1", "dataset_version": 1}))
    return features, labels


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "dataset"
        self.features, self.labels = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_confusion_and_undefined_precision(self):
        y = np.asarray([0, 0, 1, 1])
        score = np.asarray([.1, .9, .8, .2])
        metrics = evaluate(y, score, score > .5)
        self.assertEqual(metrics["confusion_matrix"], {"tn": 1, "fp": 1, "fn": 1, "tp": 1})
        for name in ("accuracy", "precision", "recall", "f1", "fpr"):
            self.assertEqual(metrics[name], .5)
        self.assertAlmostEqual(metrics["pr_auc_average_precision"], 7 / 12)
        self.assertIsNone(evaluate(y, score, score > 2)["precision"])

    def test_quantile_ties_are_not_false_positives(self):
        scores = np.asarray([1, 2, 2, 2])
        threshold = calibration_threshold(scores, .01)
        self.assertEqual(threshold, 2)
        self.assertFalse((scores > threshold).any())

    def test_identifiers_labels_and_profiles_excluded(self):
        dataset = load_dataset(self.root)
        self.assertEqual(dataset.improved.shape, (50, 12))
        self.assertEqual(dataset.reference.shape, (50, 7))
        self.assertTrue((dataset.reference[:, 0] == dataset.reference[:, 1]).all())
        self.assertEqual(dataset.counts["test"]["attack"], 2)

    def test_extra_label_feature_rejected(self):
        for row in self.features:
            row["label"] = 1
        write(self.root / "features.csv", ("window_id", *FEATURES, *EXTRA, "label"), self.features)
        with self.assertRaisesRegex(ValueError, "column contract"):
            load_dataset(self.root)

    def test_time_overlap_rejected(self):
        for row in self.labels:
            if row["split"] == "test":
                row["window_start_utc"] = "2026-10-03T00:01:00+00:00"
                row["window_end_utc"] = "2026-10-03T00:02:00+00:00"
        write(self.root / "labels.csv", sorted(LABEL_FIELDS), self.labels)
        with self.assertRaisesRegex(ValueError, "overlap"):
            load_dataset(self.root)

    def test_attack_training_rejected(self):
        self.labels[0]["label"] = 1
        write(self.root / "labels.csv", sorted(LABEL_FIELDS), self.labels)
        with self.assertRaisesRegex(ValueError, "only normal"):
            load_dataset(self.root)

    def test_missing_labels_and_duplicate_windows_rejected(self):
        write(self.root / "labels.csv", sorted(LABEL_FIELDS), self.labels[:-1])
        with self.assertRaisesRegex(ValueError, "coverage"):
            load_dataset(self.root)
        write(self.root / "labels.csv", sorted(LABEL_FIELDS), self.labels)
        write(self.root / "features.csv", ("window_id", *FEATURES, *EXTRA), [*self.features, self.features[0]])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_dataset(self.root)

    def test_nonfinite_and_failed_quality_rejected(self):
        self.features[0]["mean_interval_s"] = float("nan")
        write(self.root / "features.csv", ("window_id", *FEATURES, *EXTRA), self.features)
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            load_dataset(self.root)
        self.features[0]["mean_interval_s"] = 1
        write(self.root / "features.csv", ("window_id", *FEATURES, *EXTRA), self.features)
        (self.root / "quality.json").write_text(json.dumps({"pass": False, "status": "fail"}))
        with self.assertRaisesRegex(ValueError, "quality gate"):
            load_dataset(self.root)

    def test_real_fit_only_sees_train_and_never_overwrites(self):
        fits = []
        original_fit = MinMaxScaler.fit
        def observed_fit(self, X, y=None):
            fits.append(np.copy(X))
            return original_fit(self, X, y)
        output = Path(self.temp.name) / "run"
        with patch.object(MinMaxScaler, "fit", observed_fit):
            report = run(self.root, output, threads=1)
        self.assertEqual(len(fits), 3)
        self.assertTrue(all(matrix.shape[0] == 20 for matrix in fits))
        self.assertTrue(all(matrix[:, 0].max() < 100 for matrix in fits))
        self.assertEqual(len(report["models"]), 3)
        self.assertTrue(all(metric["windows"] == 10 for arm in report["models"].values() for metric in arm.values()))
        with self.assertRaises(FileExistsError):
            run(self.root, output)


if __name__ == "__main__":
    unittest.main()
