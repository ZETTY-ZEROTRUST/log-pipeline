#!/usr/bin/env bash
# ZETI Track B — reseed + 색인 검증 자동화 (ELK 박스 10.0.41.10 에서 실행)
#
# seed-baseline.py 로 정상 baseline 트래픽을 재주입하고 filebeat-* 색인을 검증한다.
# degenerate baseline 해소 — B1 IP공유 / B2 5캐리어 ASN / B3 200유저 / B4 페르소나.
#
# 실행 (ELK SSM 안):
#   bash scripts/reseed-and-verify.sh --clean                    # 옛 seed 비우고 재주입
#   bash scripts/reseed-and-verify.sh --hours 168 --scenario normal
#   (--clean 은 첫 인자여야 함. 나머지 인자는 그대로 seed-baseline.py 로 넘어감)
#
# 다음 단계(UBA 박스 pipeline.py)는 스크립트 끝에 안내 출력.

set -uo pipefail   # -e 제외 — 검증 FAIL 이어도 다음 단계 안내까지 출력

ES="https://10.0.41.10:9200"
ES_AUTH="elastic:Qx74mrJEwWv3E++6F-AY"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

curl_es() { curl -sk -u "$ES_AUTH" "$@"; }

# --clean: 옛 seed 누적 제거 (zeti_seed 가 dynamic:false 라 쿼리로 못 거름 →
# match_all 삭제). 재seed 시 옛 단조 데이터가 새 데이터에 섞이는 걸 막는다. opt-in.
if [ "${1:-}" = "--clean" ]; then
  shift
  echo "=== [0] --clean: 기존 filebeat-* doc 전체 삭제 ==="
  curl_es -X POST "$ES/filebeat-*/_delete_by_query?conflicts=proceed&refresh=true&wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{"query":{"match_all":{}}}' | head -c 300
  echo; echo
fi

echo "=== [1/3] 재seed 전 filebeat-* doc count ==="
BEFORE=$(curl_es "$ES/filebeat-*/_count" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])')
echo "    before = $BEFORE"
echo

echo "=== [2/3] seed-baseline.py 실행 ==="
python3 "$SCRIPT_DIR/seed-baseline.py" "$@" || { echo "seed 실패 — 중단"; exit 1; }
echo

echo "=== [3/3] filebeat-* 색인 검증 (최근 7일 범위) ==="
sleep 2
AFTER=$(curl_es "$ES/filebeat-*/_count" | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])')
echo "    doc count: $BEFORE -> $AFTER  (+$((AFTER - BEFORE)))"

RESULT=$(curl_es "$ES/filebeat-*/_search?size=0" -H 'Content-Type: application/json' -d '{
  "query": {"range": {"@timestamp": {"gte": "now-7d/h"}}},
  "aggs": {
    "subs":    {"cardinality": {"field": "jwt.sub"}},
    "asn":     {"terms": {"field": "ip_asn", "size": 10}},
    "ipclass": {"terms": {"field": "ip_class", "size": 10}}
  }}')

VERIFY_RC=0
echo "$RESULT" | python3 -c '
import sys, json
r = json.load(sys.stdin)
a = r["aggregations"]
subs = a["subs"]["value"]
asns = sorted(b["key"] for b in a["asn"]["buckets"])
classes = sorted(b["key"] for b in a["ipclass"]["buckets"])
print(f"    jwt.sub 다양성 : {subs}")
print(f"    ip_asn         : {asns}")
print(f"    ip_class       : {classes}")
ok = True
if subs < 200:
    print("    [FAIL] jwt.sub 다양성 < 200 — B3 미달"); ok = False
if len(asns) < 5:
    print("    [WARN] ip_asn 캐리어 5개 미만 — B2 확인 필요")
if classes and classes != ["cgnat_kr"]:
    print(f"    [WARN] ip_class 에 cgnat_kr 외 값 (과거 공격 데이터 잔존 가능): {classes}")
sys.exit(0 if ok else 1)
' || VERIFY_RC=1

echo
if [ "$VERIFY_RC" -eq 0 ]; then
  echo "  ✅ filebeat-* 색인 검증 통과"
else
  echo "  ❌ 검증 실패 — 위 [FAIL] 항목 확인"
fi

echo
echo "=== 다음 단계 — UBA 박스 (10.0.41.20) SSM 에서 ==="
echo "  python3 pipeline.py --hours 168          # baseline 재산출 + 채점"
echo "  검증:"
echo "   - uba-baseline    : ip_user_diversity_5min.std > 0  (degeneracy 해소)"
echo "   - uba-risk-scores : 정상 트래픽 알람 0건             (G-3 회귀)"

exit $VERIFY_RC
