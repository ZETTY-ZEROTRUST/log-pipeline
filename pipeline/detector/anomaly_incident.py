"""anomaly_incident.py — v2 탐지(anomaly-detection/1.0) → incident 묶기 → 보고 필드 → Slack 리포트.

역할: LLM은 '공격 판정자'가 아니라 '탐지 결과·근거를 정리하는 분석 보조'다. 이 모듈은
  1) 같은 subject의 관련 anomaly를 incident로 묶고(경보 개별 남발 방지),
  2) 보고 필수 필드(대상·시간·탐지결과·현재행동/비교기준/추가근거·대응결과·확인경로)를 결정론적으로 조립하고,
  3) 확정 표현을 제한하고 입력을 새니타이즈한 뒤,
  4) 요약은 LLM(주입)으로 쓰되, LLM이 없거나 실패해도 결정론 폴백으로 보고가 나가게 한다.

원칙(계약과 일치):
- anomaly_score는 공격 확률이 아니다. evidence는 관측 대 기준 비교일 뿐 인과가 아니다.
- 자동 대응은 정책 코드(response-command/I-04)가 결정한다. 이 모듈은 그 '결과'만 옮긴다.
- 토큰 원문·개인정보는 입력에 없어야 하며, 문자열은 명령이 아닌 분석 데이터로 다룬다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# 방어적: JWT compact / Bearer 접두사가 섞이면 거부(계약이 이미 금지하지만 이중 차단).
_FORBIDDEN = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.|[Bb][Ee][Aa][Rr][Ee][Rr] ")


def _scrub(value: Any) -> Any:
    """문자열이면 금지 패턴 검사(원문 토큰 유입 차단). 숫자/None은 그대로."""
    if isinstance(value, str) and _FORBIDDEN.search(value):
        raise ValueError("raw token/credential-like string in detector input")
    return value


@dataclass
class Incident:
    subject_key: str
    incident_id: str
    window_start: str
    window_end: str
    detection_ids: list[str] = field(default_factory=list)
    max_score: float = 0.0
    threshold: float | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


def incident_id_of(subject_key: str, window_start: str) -> str:
    """subject + 최초 window로 안정적 incident id. 같은 사건의 갱신은 같은 Slack 스레드로."""
    return hashlib.sha256(f"{subject_key}|{window_start}".encode()).hexdigest()[:16]


def group_incidents(records: list[dict[str, Any]]) -> list[Incident]:
    """anomaly-detection/1.0 레코드들을 subject별로 묶는다(is_anomaly=True만). 창은 min~max."""
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("status") != "EVALUATED" or not r.get("is_anomaly"):
            continue
        key = _scrub(r["target_key"]["key"])
        by_subject.setdefault(key, []).append(r)
    incidents: list[Incident] = []
    for key, rs in by_subject.items():
        rs.sort(key=lambda r: (r.get("window") or {}).get("start", ""))
        start = (rs[0].get("window") or {}).get("start", "")
        end = max(((r.get("window") or {}).get("end", "") for r in rs), default="")
        inc = Incident(subject_key=key, incident_id=incident_id_of(key, start),
                       window_start=start, window_end=end)
        for r in rs:
            inc.detection_ids.append(r["detection_id"])
            inc.max_score = max(inc.max_score, float(r.get("anomaly_score") or 0.0))
            inc.threshold = r.get("threshold")
            for e in r.get("evidence", []):
                inc.evidence.append({k: _scrub(v) for k, v in e.items()})
        incidents.append(inc)
    return incidents


def build_report_fields(inc: Incident, *, response_status: str | None = None,
                        log_link: str | None = None) -> dict[str, Any]:
    """스펙의 보고 필수 필드를 결정론적으로 조립. 확정 표현 없음(관측/기준 비교만)."""
    # 관측/기준 비율이 큰 순으로 근거 정렬(원인 단정 아님 — 관측 크기 순).
    def ratio(e):
        rv = e.get("reference_value")
        ov = e.get("observed_value")
        return (ov / rv) if (rv not in (None, 0) and ov is not None) else 0.0
    ev = sorted(inc.evidence, key=ratio, reverse=True)
    return {
        "subject_key": inc.subject_key,               # 가명(신원 아님)
        "incident_id": inc.incident_id,
        "window": f"{inc.window_start} ~ {inc.window_end}",
        "anomaly_score": round(inc.max_score, 4),      # 공격 확률 아님
        "threshold": inc.threshold,
        "evidence": [                                  # 관측 vs 기준(인과 아님)
            {"feature": e.get("feature"), "unit": e.get("unit"),
             "observed": e.get("observed_value"),
             "reference": e.get("reference_value"),
             "reference_statistic": e.get("reference_statistic"),
             "reference_population": e.get("reference_population"),
             "ratio": round(ratio(e), 2)} for e in ev
        ],
        "response_status": response_status or "대응 없음(관측·보고 단계)",
        "detection_ids": inc.detection_ids,
        "log_link": log_link or f"(로그 링크 미지정 · incident {inc.incident_id})",
    }


def deterministic_summary(fields: dict[str, Any]) -> str:
    """LLM이 없거나 실패해도 나가는 폴백 요약. 확정 표현 없이 관측·기준만."""
    top = fields["evidence"][0] if fields["evidence"] else None
    line = f"{fields['window']}에 사용자 {fields['subject_key']}의 평소와 다른 접근이 관측됐습니다."
    if top and top.get("reference") not in (None, 0):
        line += (f" {top['feature']}가 {top['observed']}로 기준({top['reference_statistic']} "
                 f"{top['reference']}, {top['reference_population']})의 약 {top['ratio']}배입니다.")
    line += (f" 이상 점수 {fields['anomaly_score']}로 경보 기준 {fields['threshold']}를 넘었습니다"
             f"(점수는 공격 확률이 아닙니다).")
    return line


def format_slack(fields: dict[str, Any], summary: str) -> str:
    """스펙 예시 형태의 Slack 보고 텍스트(확정 표현 없음 · 확인 요청)."""
    return (
        f"[행동 이상 감지 · 확인 필요] 사용자 {fields['subject_key']}\n"
        f"{summary}\n"
        f"처리 상태: {fields['response_status']}\n"
        f"확인 사항: 정상 배치 작업이나 대량 조회 업무인지 확인이 필요합니다.\n"
        f"상세 기록: 사건 {fields['incident_id']} · {fields['log_link']}"
    )


def report_incident(inc: Incident, *, response_status: str | None = None,
                    log_link: str | None = None,
                    llm_summarize: Callable[[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    """incident → (필드, Slack 텍스트). LLM 주입 시 요약을 LLM이, 실패/부재 시 결정론 폴백.

    LLM 장애가 보고를 막지 않는다(자동 대응은 이 경로가 아니라 정책 코드 소관).
    """
    fields = build_report_fields(inc, response_status=response_status, log_link=log_link)
    summary = None
    if llm_summarize is not None:
        try:
            summary = llm_summarize(fields)
        except Exception:
            summary = None
    if not summary:
        summary = deterministic_summary(fields)
    return {"fields": fields, "slack_text": format_slack(fields, summary)}


if __name__ == "__main__":
    import json
    import sys
    # dry-run: anomaly-detection/1.0 레코드 배열 JSON을 받아 incident별 Slack 텍스트 출력(LLM 없이 폴백).
    records = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else [{
        "schema_version": "anomaly-detection/1.0", "detection_id": "d" * 64, "status": "EVALUATED",
        "target_key": {"key": "TqAeVuSuTeOKYwDd", "key_version": "1"},
        "window": {"start": "2026-09-27T21:00:00Z", "end": "2026-09-27T21:05:00Z"},
        "anomaly_score": 0.78, "threshold": 0.70, "threshold_operator": "GT", "is_anomaly": True,
        "evidence": [{"feature": "request_count", "unit": "COUNT", "observed_value": 450,
                      "reference_statistic": "MEAN", "reference_value": 30, "reference_population": "TARGET_HISTORY"},
                     {"feature": "distinct_routes", "unit": "DISTINCT_COUNT", "observed_value": 12,
                      "reference_statistic": "P95", "reference_value": 3, "reference_population": "TARGET_HISTORY"}],
    }]
    for inc in group_incidents(records):
        out = report_incident(inc, response_status="추가 인증 요청 완료. 세션 회수는 실행하지 않았습니다.")
        print(out["slack_text"]); print("-" * 60)
