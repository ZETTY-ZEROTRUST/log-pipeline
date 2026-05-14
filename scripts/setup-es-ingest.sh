#!/usr/bin/env bash
# setup-es-ingest.sh — ELK 머신 SSM 안에서 한 번 돌리면 Phase 0.5 ES 측 작업 다 끝남
#
# 1. sanity check (cluster health + ES version)
# 2. 충돌 검사 (asn-classify / filebeat-uba-final / filebeat-jwt 이름이 이미 있나. jwt-decode는 v11에서 폐기됨 — Filebeat로 이전)
# 3. 환경 정찰 (filebeat-* 인덱스 / GeoLite2 DB 배치 여부 / Filebeat 설치 상태)
# 4. 충돌 없으면 자동 PUT 4개 (있으면 PROCEED=yes 안 박으면 중단)
# 5. verify 스크립트 2개 실행 (jwt + asn)
#
# 사용법 (ELK 머신 SSM 세션 안에서):
#   export ES_HOST="https://localhost:9200"
#   export ES_USER="elastic"
#   export ES_PASS="..."
#   cd /tmp && git clone https://github.com/ZETTY-ZEROTRUST/log-pipeline.git
#   cd log-pipeline
#   bash scripts/setup-es-ingest.sh
#
# 충돌 시 덮어쓰려면:
#   PROCEED=yes bash scripts/setup-es-ingest.sh

set -uo pipefail

ES_HOST="${ES_HOST:?ES_HOST 환경변수 필요}"
ES_USER="${ES_USER:?ES_USER 환경변수 필요}"
ES_PASS="${ES_PASS:?ES_PASS 환경변수 필요}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_section() {
    echo ""
    echo -e "${BLUE}=============================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}=============================================================${NC}"
}

# ============================================================================
# Step 1 — Sanity check
# ============================================================================
log_section "Step 1: ES 클러스터 sanity check"
HEALTH=$(curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cluster/health" 2>/dev/null)
STATUS=$(echo "$HEALTH" | jq -r '.status' 2>/dev/null || echo "unknown")
case "$STATUS" in
    green|yellow)
        echo -e "${GREEN}OK${NC}  cluster status: $STATUS"
        ;;
    red)
        echo -e "${RED}FAIL${NC} cluster status: red — ELK 노드 점검 필요"
        exit 1
        ;;
    *)
        echo -e "${RED}FAIL${NC} 응답 비정상: $HEALTH"
        echo "       → ES_PASS 틀림 또는 ES 서비스 안 떠있음"
        exit 1
        ;;
esac

VERSION=$(curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/" 2>/dev/null | jq -r '.version.number' 2>/dev/null)
echo "ES version: $VERSION"

# ============================================================================
# Step 2 — 충돌 검사
# ============================================================================
log_section "Step 2: 충돌 검사 (덮어쓰기 사고 방지)"
CONFLICT=0

for name in asn-classify filebeat-uba-final; do
    RESULT=$(curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_ingest/pipeline/$name" 2>/dev/null)
    if echo "$RESULT" | grep -q "resource_not_found_exception"; then
        echo -e "${GREEN}NEW${NC}    ingest pipeline: $name"
    elif echo "$RESULT" | grep -q "\"$name\""; then
        echo -e "${YELLOW}EXISTS${NC} ingest pipeline: $name (덮어쓰기 위험)"
        CONFLICT=$((CONFLICT+1))
    else
        echo -e "${RED}???${NC}    ingest pipeline: $name (응답 비정상)"
    fi
done

TEMPLATE_RESULT=$(curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_index_template/filebeat-jwt" 2>/dev/null)
if echo "$TEMPLATE_RESULT" | grep -q "resource_not_found_exception"; then
    echo -e "${GREEN}NEW${NC}    index template: filebeat-jwt"
elif echo "$TEMPLATE_RESULT" | grep -q "filebeat-jwt"; then
    echo -e "${YELLOW}EXISTS${NC} index template: filebeat-jwt (덮어쓰기 위험)"
    CONFLICT=$((CONFLICT+1))
fi

# ============================================================================
# Step 3 — 환경 정찰 (정보 수집)
# ============================================================================
log_section "Step 3: 환경 정찰"

echo "--- filebeat / uba 관련 기존 인덱스 ---"
INDEXES=$(curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cat/indices?v" 2>/dev/null | grep -E "filebeat|uba|^health" || echo "")
if [ -z "$INDEXES" ]; then
    echo "(없음 — 깨끗한 환경)"
else
    echo "$INDEXES"
fi

echo ""
echo "--- GeoLite2-ASN.mmdb 위치 ---"
GEOIP_DB=$(sudo find /etc/elasticsearch /usr/share/elasticsearch /var/lib/elasticsearch -name "GeoLite2-ASN.mmdb" 2>/dev/null | head -1)
if [ -n "$GEOIP_DB" ]; then
    echo -e "${GREEN}배치됨${NC}: $GEOIP_DB"
else
    echo -e "${YELLOW}미배치${NC}: asn-classify의 ASN 매칭이 silent fail로 떨어짐 (ip_class=unknown)"
    echo "             → MaxMind 가입 + GeoLite2-ASN.mmdb 다운 + 별도 배치 필요"
fi

echo ""
echo "--- Filebeat 설치/상태 ---"
if command -v filebeat &>/dev/null; then
    echo -e "${GREEN}installed${NC}: $(which filebeat)"
    systemctl is-active filebeat 2>/dev/null && echo "    service: active" || echo "    service: inactive 또는 미등록"
else
    echo -e "${YELLOW}미설치${NC} (이 머신은 ELK라 정상 — Filebeat는 priv-web 머신에 설치 예정)"
fi

# ============================================================================
# Step 4 — 진행 결정
# ============================================================================
log_section "Step 4: 진행 결정"
if [ $CONFLICT -gt 0 ]; then
    echo -e "${YELLOW}충돌 $CONFLICT 건 발견${NC}"
    if [ "${PROCEED:-}" != "yes" ]; then
        echo ""
        echo "기존 pipeline/template을 덮어쓰지 않으려면 종료."
        echo "덮어쓰려면 다음 명령으로 재실행:"
        echo "  PROCEED=yes bash $0"
        exit 2
    fi
    echo -e "${YELLOW}PROCEED=yes 박힘 — 덮어쓰기 진행${NC}"
else
    echo -e "${GREEN}충돌 없음 — 자동 PUT 진행${NC}"
fi

# ============================================================================
# Step 5 — PUT 4개
# ============================================================================
log_section "Step 5: PUT (ingest pipeline 3 + index template 1)"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( dirname "$SCRIPT_DIR" )"

PUT_FAIL=0
for f in asn-classify filebeat-uba-final; do
    echo -n "PUT pipeline/$f ... "
    RESP=$(curl -sk -u "$ES_USER:$ES_PASS" -H "Content-Type: application/json" \
        -X PUT "$ES_HOST/_ingest/pipeline/$f" \
        --data @"$REPO_ROOT/es-pipelines/$f.json" 2>&1)
    if echo "$RESP" | grep -q '"acknowledged":true'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
        echo "       응답: $RESP" | head -c 400
        echo ""
        PUT_FAIL=$((PUT_FAIL+1))
    fi
done

echo -n "PUT index template/filebeat-jwt ... "
RESP=$(curl -sk -u "$ES_USER:$ES_PASS" -H "Content-Type: application/json" \
    -X PUT "$ES_HOST/_index_template/filebeat-jwt" \
    --data @"$REPO_ROOT/es-mappings/filebeat-jwt-template.json" 2>&1)
if echo "$RESP" | grep -q '"acknowledged":true'; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAIL${NC}"
    echo "       응답: $RESP" | head -c 400
    PUT_FAIL=$((PUT_FAIL+1))
fi

if [ $PUT_FAIL -gt 0 ]; then
    echo ""
    echo -e "${RED}PUT $PUT_FAIL 건 실패 — verify 단계 진입 안 함${NC}"
    exit 3
fi

# ============================================================================
# Step 6 — verify 스크립트
# ============================================================================
log_section "Step 6: verify 스크립트 실행"
echo "--- verify_jwt_decode.sh ---"
bash "$SCRIPT_DIR/verify_jwt_decode.sh" || true

echo ""
echo "--- verify_asn_classify.sh ---"
bash "$SCRIPT_DIR/verify_asn_classify.sh" || true

# ============================================================================
# 완료
# ============================================================================
log_section "완료"
echo "Phase 0.5의 ES 측 작업 완료."
echo ""
echo "남은 작업:"
echo "  - MaxMind GeoLite2-ASN.mmdb 다운 + 이 머신 배치 (현재 미배치 시 ASN 매칭이 unknown으로 떨어짐)"
echo "  - Nginx 머신(2a/2b)에 uba.conf SSM 배치 + nginx -s reload"
echo "  - Filebeat 설치 + filebeat.yml 배치 + systemctl start"
echo "  - 실 cURL 트래픽 수십 건 → filebeat-* 인덱스 색인 확인"
echo "  - mock baseline bulk insert로 통계 팩터 baseline 형성"
