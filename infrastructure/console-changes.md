# console-changes.md

AWS 콘솔 / ES API / SSM 으로 박스에 직접 친 변경 기록.
Terraform 이전 시 이 파일이 base.

형식: 날짜 / 변경 대상 / before / after / 이유 / 검증.

---

## 2026-05-24 — uba-alerts / uba-intelligence 매핑 Option B 적용 + legacy 정리

**변경 대상**: ELK 박스 (10.0.41.10) ES `_index_template`

**before**:
- `uba-alerts` 템플릿: `dynamic: "strict"` + `llm_report` 의 6 sub-필드 명시 (behavior_analysis text / attack_phase keyword / similar_cases / nat_assessment / recommended_actions / confidence)
- `uba-intelligence` 템플릿: `dynamic: "strict"` + `llm_report` 의 3 sub-필드 명시 (campaign_summary / pattern_analysis / narrative)
- `uba-alerts-template` (legacy, `uba-alerts-*` 패턴) 잔존 → priority 0 끼리 충돌 가능

**after**:
- `PUT _index_template/uba-alerts` — `dynamic: true` + `llm_report: {type:object, enabled:false}`
- `PUT _index_template/uba-intelligence` — 동일
- `DELETE _index_template/uba-alerts-template` (legacy 제거)

**이유**:
1. Option B = 매핑 동기화 burden 회피. LLM prompt 변경 시 sub-필드 추가/이름 변경되면 strict 매핑은 doc write 거부. dynamic:true 로 자동 매핑.
2. `llm_report` 의 `enabled: false` = `_source` 에는 그대로 저장 (Kibana 패널 표시 가능) + 역색인 안 만듦 (한국어 토크나이징 비용 0, 검색/집계 X). LLM 본문은 사람이 *읽는* 용이지 검색 안 함.
3. legacy `uba-alerts-template` 는 새 `uba-alerts` 와 같은 인덱스 패턴 매치 → priority 충돌 회피.

**검증**:
- `PUT acknowledged:true` 두 번 (uba-alerts, uba-intelligence)
- `GET _index_template/uba-alerts` 결과: `dynamic:true, llm_report:{type:object,enabled:false}` 박힘 확인
- 잔존 6 템플릿: uba-user-profiles / uba-intelligence / uba-logs-template / uba-alerts / uba-events / uba-risk-scores

**잔재 의문** (발표 후 정리):
- `uba-logs-template` (legacy 가능) — 사용처 미확인
- ILM policy `uba-alerts-policy`, `uba-intelligence-policy` 미생성 → 단발 시연엔 무관, 운영 시 rollover 안 돌
- legacy template 삭제는 ES 에 즉시 적용 — 기존 인덱스 (현재 없음) 의 매핑은 변경 안 됨, *새 인덱스 생성 시* 만 영향

**적용 명령 (이력)**:
```
PUT https://10.0.41.10:9200/_index_template/uba-alerts          # body = es-mappings/uba-alerts.json
PUT https://10.0.41.10:9200/_index_template/uba-intelligence    # body = es-mappings/uba-intelligence.json
DELETE https://10.0.41.10:9200/_index_template/uba-alerts-template
```
