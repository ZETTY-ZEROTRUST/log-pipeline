"""Historical aggregation semantics, independent fixtures and artifact checks."""
from pathlib import Path
import tempfile
import unittest

import numpy as np

import baseline


def line(ip, date, size, method="GET", status=200, timezone="+0000"):
    return f'{ip} - - [{date} {timezone}] "{method} /fixture HTTP/1.1" {status} {size} "-" "fixture"\n'


class FeaturesTest(unittest.TestCase):
    def test_active_days_first_zero_and_sample_std(self):
        logs = [line("10.0.0.1", "01/Jan/2020:00:00:10", "300", "POST", 404),
                line("10.0.0.1", "03/Jan/2020:00:00:00", "-"),
                line("10.0.0.1", "01/Jan/2020:00:00:00", "100"), "invalid\n"]
        _, matrix, quality = baseline.aggregate_lines(logs)
        # Active days=2; gaps are [0,10,0]; std uses ddof=1, missing bytes=0.
        np.testing.assert_allclose(matrix[0], [3, 1.5, 2, 2, 400 / 3,
                                             np.std([100, 300, 0], ddof=1), 10 / 3])
        self.assertEqual(quality, {"lines": 4, "parsed": 3, "skipped": 1,
                                   "missing_bytes_mapped_to_zero": 1})

    def test_singleton_timezone_discarded_and_order(self):
        logs = [line("10.0.0.2", "01/Jan/2020:00:00:00", "2"),
                line("10.0.0.1", "01/Jan/2020:00:00:10", "4", timezone="+0900"),
                line("10.0.0.1", "01/Jan/2020:00:00:00", "4", timezone="-0500")]
        ips, matrix, _ = baseline.aggregate_lines(logs)
        np.testing.assert_allclose(matrix[0], [2, 2, 2, 2, 4, 0, 5])
        np.testing.assert_allclose(matrix[1], [1, 1, 1, 1, 2, 0, 0])
        other_ips, other, _ = baseline.aggregate_lines(reversed(logs))
        self.assertEqual(ips, other_ips)
        np.testing.assert_array_equal(matrix, other)

    def test_no_matching_records_fail(self):
        with self.assertRaises(ValueError):
            baseline.aggregate_lines(["invalid\n"])

    def test_malformed_matching_record_omits_raw_values(self):
        raw = line("10.0.0.1", "not-a-date", "private-value")
        with self.assertRaisesRegex(ValueError, "raw values omitted") as raised:
            baseline.aggregate_lines([raw])
        self.assertNotIn("private-value", str(raised.exception))

    def test_raw_identity_not_exported_and_deterministic_flags(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / "public.log"
            path.write_text("".join(line(f"10.0.0.{i}", "01/Jan/2020:00:00:00", str(i * 10))
                                    for i in range(1, 15)))
            first = baseline.run(path, root / "one", models=("iforest",))
            second = baseline.run(path, root / "two", models=("iforest",))
            predictions = (root / "one" / "predictions.csv").read_text()
            self.assertNotIn("10.0.0.", predictions)
            self.assertEqual(predictions, (root / "two" / "predictions.csv").read_text())
            self.assertEqual(first["detection_metrics"]["fpr"], None)
            self.assertEqual(first["models"]["iforest"]["flagged_subjects"],
                             second["models"]["iforest"]["flagged_subjects"])
            with self.assertRaises(FileExistsError):
                baseline.run(path, root / "one")


if __name__ == "__main__":
    unittest.main()
