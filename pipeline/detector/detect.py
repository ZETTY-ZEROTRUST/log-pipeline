"""HTTP 행위 이상 탐지(detector_id=http-behavior). 비지도 IsolationForest.

입력: security-event/2.0 ACCESS_DECISION을 subject_key별로 집계한 피처(ES agg 덤프).
출력: anomaly-detection/1.0 레코드(계약 준수, detection_id/input_hash 결정적) + 평가 요약
      + 고신뢰 이상에 대한 response-command/1.0(DRY_RUN, 정책 산출물).

가명(subject_key)만 다루며 원본 신원·secret은 읽지 않는다. 라벨은 학습에 쓰지 않고
평가(ground truth)에만 쓴다 — 운영에서 라벨은 없다는 UBA 전제를 지킨다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

DOMAIN = "zetty:anomaly-detection:id:v1"
DETECTOR_ID = "http-behavior"
ENVIRONMENT = "local-lab"
FEATURE_VERSION = "http-feat-1"
MODEL_VERSION = "http-if-2026.09.27"
THRESHOLD_VERSION = "http-thr-1"
KEY_VERSION = "1"

# 학습에 쓰는 피처(feature_version=http-feat-1). subject_key별 window 집계에서 뽑는다.
FEATURES = [
    "request_count",
    "request_rate_per_s",
    "distinct_routes",
    "distinct_sessions",
    "burst_max_per_s",
]
# evidence로 보고할 피처(관측 vs 코호트 P95). 단위는 계약 enum.
EVIDENCE = [
    ("request_count", "COUNT"),
    ("distinct_routes", "DISTINCT_COUNT"),
    ("burst_max_per_s", "COUNT"),
]


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def load_subjects(raw_path: Path, window_seconds: float) -> list[dict]:
    doc = json.loads(raw_path.read_text())
    buckets = doc["aggregations"]["by_subject"]["buckets"]
    subs = []
    for b in buckets:
        req = int(b["doc_count"])
        burst = (b.get("burst_max") or {}).get("value") or 0
        subs.append({
            "subject_key": b["key"],
            "request_count": float(req),
            "request_rate_per_s": req / window_seconds,
            "distinct_routes": float(b["routes"]["value"]),
            "distinct_sessions": float(b["sessions"]["value"]),
            "burst_max_per_s": float(burst),
            "sensitive_read_count": float(b["sensitive"]["doc_count"]),
            "error_count": float(b["errors"]["doc_count"]),
        })
    return subs


def input_hash(sub: dict) -> str:
    """detector 입력 집합의 sha256. 같은 입력 재평가는 같은 값 → 같은 detection_id."""
    payload = {
        "feature_version": FEATURE_VERSION,
        "model_version": MODEL_VERSION,
        "threshold_version": THRESHOLD_VERSION,
        "features": {k: sub[k] for k in FEATURES},
    }
    return "sha256:" + hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def derive_detection_id(rec: dict) -> str:
    window = rec.get("window") or {}
    origin = rec["data_origin"]
    fields = [
        DOMAIN, rec["environment"], origin["kind"], origin["dataset_id"] or "",
        rec["detector_id"], rec["target_type"], rec["target_key"]["key_version"], rec["target_key"]["key"],
        window.get("start") or "", window.get("end") or "", rec["event_ref"] or "",
        rec["feature_version"], rec["model_version"], rec["threshold_version"], rec["input_hash"],
    ]
    return hashlib.sha256("\n".join(fields).encode("utf-8")).hexdigest()


def build_record(sub, score, threshold, is_anom, cohort_p95, window, cutoff, detected_at):
    rec = {
        "schema_version": "anomaly-detection/1.0",
        "detection_id": "",  # 아래에서 채운다
        "detector_id": DETECTOR_ID,
        "environment": ENVIRONMENT,
        "data_origin": {"kind": "SERVICE_EVENTS", "dataset_id": None},
        "target_type": "SUBJECT",
        "target_key": {"key": sub["subject_key"], "key_version": KEY_VERSION},
        "window": {"start": window[0], "end": window[1]},
        "event_ref": None,
        "feature_version": FEATURE_VERSION,
        "model_version": MODEL_VERSION,
        "threshold_version": THRESHOLD_VERSION,
        "input_hash": input_hash(sub),
        "cutoff": cutoff,
        "status": "EVALUATED",
        "status_reason": "NONE",
        "anomaly_score": round(float(score), 6),
        "threshold": round(float(threshold), 6),
        "threshold_operator": "GT",
        "is_anomaly": bool(is_anom),
        "evidence": [
            {
                "feature": feat,
                "unit": unit,
                "observed_value": round(float(sub[feat]), 6),
                "reference_statistic": "P95",
                "reference_value": round(float(cohort_p95[feat]), 6),
                "reference_population": "COHORT",
            }
            for feat, unit in EVIDENCE
        ],
        "detected_at": detected_at,
    }
    rec["detection_id"] = derive_detection_id(rec)
    return rec


def build_response_command(rec, requested_at, expires_at):
    """정책: 고신뢰 이상 → REVOKE_SESSION(DRY_RUN). basis=detection_id, evidence_ref로 대조."""
    det = rec["detection_id"]
    return {
        "schema_version": "response-command/1.0",
        "command_id": str(uuid.uuid4()),
        "detection_id": det,
        "incident_id": None,
        "policy_version": "uba-policy-2026.09",
        "action": "REVOKE_SESSION",
        "target_type": "SUBJECT",
        "target_key": {"key": rec["target_key"]["key"], "key_version": rec["target_key"]["key_version"]},
        "environment": ENVIRONMENT,
        "requested_at": requested_at,
        "expires_at": expires_at,
        "mode": "DRY_RUN",
        "evidence_ref": "anomaly-detection:" + det,
        "expected_state_version": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--window-start", default="2026-09-27T06:42:45Z")
    ap.add_argument("--window-end", default="2026-09-27T06:43:18Z")
    ap.add_argument("--cutoff", default="2026-09-27T06:43:48Z")
    ap.add_argument("--detected-at", default="2026-09-27T06:44:00Z")
    ap.add_argument("--ground-truth", default=None)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--contamination", type=float, default=0.03125)  # 사전확률 ~1/32
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    from datetime import datetime, timezone
    def secs(a, b):
        f = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds()
    window_seconds = secs(args.window_start, args.window_end)

    subs = load_subjects(Path(args.raw), window_seconds)
    X = np.array([[s[f] for f in FEATURES] for s in subs], dtype=float)
    Xs = StandardScaler().fit_transform(X)

    clf = IsolationForest(n_estimators=200, contamination=args.contamination, random_state=args.seed)
    clf.fit(Xs)
    # 방향 고정: 클수록 이상. threshold=0, GT → is_anomaly == (predict==-1).
    scores = -clf.decision_function(Xs)
    threshold = 0.0
    flags = scores > threshold

    cohort_p95 = {f: float(np.percentile([s[f] for s in subs], 95)) for f in FEATURES + ["request_count", "distinct_routes", "burst_max_per_s"]}

    window = (args.window_start, args.window_end)
    records = [
        build_record(subs[i], scores[i], threshold, flags[i], cohort_p95, window, args.cutoff, args.detected_at)
        for i in range(len(subs))
    ]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "anomaly_detections.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))

    # 정책: 이상으로 판정된 subject에 대해 DRY_RUN 대응 명령.
    commands = [
        build_response_command(records[i], args.detected_at, "2026-09-27T07:43:48Z")
        for i in range(len(subs)) if flags[i]
    ]
    (out / "response_commands.json").write_text(json.dumps(commands, ensure_ascii=False, indent=2))

    # 평가(ground truth는 평가에만). 라벨 없이 학습한 결과를 사후 채점.
    summary = {
        "subjects": len(subs),
        "flagged": int(flags.sum()),
        "threshold": threshold,
        "contamination": args.contamination,
        "score_min": round(float(scores.min()), 6),
        "score_max": round(float(scores.max()), 6),
        "top": sorted(
            [{"subject_key": subs[i]["subject_key"][:16], "score": round(float(scores[i]), 4),
              "req": int(subs[i]["request_count"]), "routes": int(subs[i]["distinct_routes"]),
              "burst": int(subs[i]["burst_max_per_s"]), "flag": bool(flags[i])} for i in range(len(subs))],
            key=lambda r: -r["score"])[:5],
    }
    if args.ground_truth:
        gt = json.loads(Path(args.ground_truth).read_text())
        attacker = gt["attacker"]["subject_key"]
        idx = {s["subject_key"]: i for i, s in enumerate(subs)}
        ai = idx.get(attacker)
        tp = int(bool(flags[ai])) if ai is not None else 0
        fp = int(flags.sum()) - tp
        fn = 0 if (ai is not None and flags[ai]) else 1
        others = sorted([scores[i] for i in range(len(subs)) if i != ai], reverse=True)
        summary["eval"] = {
            "attacker_present": ai is not None,
            "attacker_flagged": bool(ai is not None and flags[ai]),
            "attacker_score": round(float(scores[ai]), 4) if ai is not None else None,
            "attacker_rank": (sorted(range(len(subs)), key=lambda i: -scores[i]).index(ai) + 1) if ai is not None else None,
            "top_normal_score": round(float(others[0]), 4) if others else None,
            "margin_attacker_vs_top_normal": round(float(scores[ai] - others[0]), 4) if (ai is not None and others) else None,
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
            "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        }
    (out / "detection_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
