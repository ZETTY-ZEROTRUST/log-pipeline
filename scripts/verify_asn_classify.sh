#!/usr/bin/env bash
# verify_asn_classify.sh — ES _simulate로 asn-classify pipeline 검증 (v11)
#
# G-2 게이트 통과 조건 (ASN 분류 측).
#
# 사용법:
#   export ES_HOST=https://10.0.41.10:9200
#   export ES_USER=...
#   export ES_PASS=...
#   bash scripts/verify_asn_classify.sh
#
# 사전조건: asn-classify pipeline이 ES에 PUT 등록 + GeoLite2-ASN.mmdb 배치 (선택)
#   - DB 배치 전: A1 (시뮬 override) + A4 (에러) 검증 가능
#   - DB 배치 후: A2 (실제 cgnat_kr ASN) + A3 (cloud ASN) 추가 검증

set -euo pipefail

ES_HOST="${ES_HOST:?ES_HOST 환경변수 필요}"
ES_USER="${ES_USER:?ES_USER 환경변수 필요}"
ES_PASS="${ES_PASS:?ES_PASS 환경변수 필요}"

PASS=0
FAIL=0
SKIP=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

run_case() {
    local name="$1"
    local input_doc="$2"
    local expected="$3"
    local jq_check="$4"

    local response actual
    response=$(curl -sk -u "$ES_USER:$ES_PASS" \
        -H "Content-Type: application/json" \
        -X POST "$ES_HOST/_ingest/pipeline/asn-classify/_simulate" \
        -d "{\"docs\": [{\"_source\": $input_doc}]}")

    actual=$(echo "$response" | jq -r "$jq_check" 2>/dev/null || echo "<jq_error>")

    if [ "$actual" = "$expected" ]; then
        printf "${GREEN}PASS${NC}  %-55s  expected=%-30s actual=%s\n" "$name" "$expected" "$actual"
        PASS=$((PASS+1))
    else
        printf "${RED}FAIL${NC}  %-55s  expected=%-30s actual=%s\n" "$name" "$expected" "$actual"
        echo "       full response: $response" | head -c 500
        echo ""
        FAIL=$((FAIL+1))
    fi
}

skip_case() {
    local name="$1"
    local reason="$2"
    printf "${YELLOW}SKIP${NC}  %-55s  reason=%s\n" "$name" "$reason"
    SKIP=$((SKIP+1))
}

echo "=== asn-classify pipeline 검증 (v11) ==="
echo ""

# === A1: S7 시뮬 IP override (DB 무관, 가장 우선순위 높음) ===
# 시뮬 IP는 PROGRESS.md / asn-classify.json에 박힌 3개
for SIM_IP in "10.0.21.62" "10.0.22.106" "10.0.21.218"; do
    run_case "A1-${SIM_IP}: 시뮬 IP → ip_class=cgnat_kr" \
        "{\"remote_addr\": \"$SIM_IP\"}" \
        "cgnat_kr" \
        '.docs[0].doc._source.ip_class'
    run_case "A1-${SIM_IP}: 시뮬 IP → is_nat_whitelisted=true" \
        "{\"remote_addr\": \"$SIM_IP\"}" \
        "true" \
        '.docs[0].doc._source.is_nat_whitelisted'
    run_case "A1-${SIM_IP}: 시뮬 IP → ip_class_source=s7_simulator_override" \
        "{\"remote_addr\": \"$SIM_IP\"}" \
        "s7_simulator_override" \
        '.docs[0].doc._source.ip_class_source'
done

# === A2: cgnat_kr 실제 ASN 매칭 (GeoLite2-ASN.mmdb 배치 시) ===
# SKT 대표 IP 1개로 테스트. GeoLite2-ASN.mmdb 배치 안 된 환경에선 SKIP.
echo ""
echo "--- DB 배치 시점에만 의미 (DB 없으면 unknown으로 떨어짐) ---"
# 211.36.x = SKT (AS9644 일반). 운영환경에서만 검증 가능.
# 일단 검증 호출하되 결과는 환경에 따라 다름
A2_RESPONSE=$(curl -sk -u "$ES_USER:$ES_PASS" \
    -H "Content-Type: application/json" \
    -X POST "$ES_HOST/_ingest/pipeline/asn-classify/_simulate" \
    -d '{"docs": [{"_source": {"remote_addr": "211.36.142.1"}}]}')
A2_CLASS=$(echo "$A2_RESPONSE" | jq -r '.docs[0].doc._source.ip_class' 2>/dev/null || echo "<jq_error>")
if [ "$A2_CLASS" = "cgnat_kr" ]; then
    printf "${GREEN}PASS${NC}  A2: SKT IP 211.36.142.1 → ip_class=cgnat_kr (GeoLite2 배치됨)\n"
    PASS=$((PASS+1))
elif [ "$A2_CLASS" = "unknown" ]; then
    skip_case "A2: SKT IP → cgnat_kr" "GeoLite2-ASN.mmdb 미배치 (현재 unknown으로 fallback)"
else
    printf "${RED}FAIL${NC}  A2: SKT IP 211.36.142.1 → expected=cgnat_kr or unknown actual=$A2_CLASS\n"
    FAIL=$((FAIL+1))
fi

# === A3: cloud (AWS) IP 매칭 ===
# 13.124.x = AWS Seoul region (AS16509)
A3_RESPONSE=$(curl -sk -u "$ES_USER:$ES_PASS" \
    -H "Content-Type: application/json" \
    -X POST "$ES_HOST/_ingest/pipeline/asn-classify/_simulate" \
    -d '{"docs": [{"_source": {"remote_addr": "13.124.1.1"}}]}')
A3_CLASS=$(echo "$A3_RESPONSE" | jq -r '.docs[0].doc._source.ip_class' 2>/dev/null || echo "<jq_error>")
if [ "$A3_CLASS" = "cloud" ]; then
    printf "${GREEN}PASS${NC}  A3: AWS IP 13.124.1.1 → ip_class=cloud (GeoLite2 배치됨)\n"
    PASS=$((PASS+1))
elif [ "$A3_CLASS" = "unknown" ]; then
    skip_case "A3: AWS IP → cloud" "GeoLite2-ASN.mmdb 미배치 (현재 unknown으로 fallback)"
else
    printf "${RED}FAIL${NC}  A3: AWS IP → expected=cloud or unknown actual=$A3_CLASS\n"
    FAIL=$((FAIL+1))
fi

# === A4: remote_addr 없음 → ip_class=unknown (geoip silent fail) ===
echo ""
run_case "A4: remote_addr 없음 → ip_class=unknown" \
    '{}' \
    "unknown" \
    '.docs[0].doc._source.ip_class'

echo ""
echo "=== 결과: PASS=$PASS / FAIL=$FAIL / SKIP=$SKIP ==="
echo ""
[ $SKIP -gt 0 ] && echo "ⓘ SKIP은 GeoLite2-ASN.mmdb 배치 후 다시 실행하면 PASS로 전환 가능"
[ $FAIL -eq 0 ] && exit 0 || exit 1
