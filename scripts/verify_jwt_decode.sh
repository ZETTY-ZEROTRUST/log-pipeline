#!/usr/bin/env bash
# verify_jwt_decode.sh — ES _simulate로 jwt-decode pipeline 4 골든 케이스 검증 (v11)
#
# G-2 게이트 통과 조건. G5 (r=0,s=0 시그니처)는 backend KMS reject 회귀로 분리 — 여기 X.
#
# 사용법:
#   export ES_HOST=https://10.0.41.10:9200
#   export ES_USER=...
#   export ES_PASS=...
#   bash scripts/verify_jwt_decode.sh
#
# 사전조건: jwt-decode pipeline이 ES에 PUT 등록되어 있어야 함
#   curl -X PUT "$ES_HOST/_ingest/pipeline/jwt-decode" -H "Content-Type: application/json" -u "$ES_USER:$ES_PASS" --data @es-pipelines/jwt-decode.json

set -euo pipefail

ES_HOST="${ES_HOST:?ES_HOST 환경변수 필요 (예: https://10.0.41.10:9200)}"
ES_USER="${ES_USER:?ES_USER 환경변수 필요}"
ES_PASS="${ES_PASS:?ES_PASS 환경변수 필요}"

PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

run_case() {
    local name="$1"
    local input_doc="$2"
    local expected="$3"
    local jq_check="$4"

    local response actual
    response=$(curl -sk -u "$ES_USER:$ES_PASS" \
        -H "Content-Type: application/json" \
        -X POST "$ES_HOST/_ingest/pipeline/jwt-decode/_simulate" \
        -d "{\"docs\": [{\"_source\": $input_doc}]}")

    actual=$(echo "$response" | jq -r "$jq_check" 2>/dev/null || echo "<jq_error>")

    if [ "$actual" = "$expected" ]; then
        printf "${GREEN}PASS${NC}  %-50s  expected=%-20s actual=%s\n" "$name" "$expected" "$actual"
        PASS=$((PASS+1))
    else
        printf "${RED}FAIL${NC}  %-50s  expected=%-20s actual=%s\n" "$name" "$expected" "$actual"
        echo "       full response: $response" | head -c 500
        echo ""
        FAIL=$((FAIL+1))
    fi
}

echo "=== jwt-decode pipeline 검증 (4 골든 케이스, G5 제외) ==="
echo ""

# === G1: 정상 ES256 토큰 ===
# header: {"alg":"ES256","kid":"test-kid","typ":"JWT"}
# payload: {"sub":"140000511","jti":"test-jti","iss":"https://auth.zeti.com/","aud":["https://api.zeti.com"],"exp":1778056993,"iat":1778056393,"auth_time":1778056393,"nbf":1778056393}
# sig: dummysignature (decode pipeline은 sig 검증 안 함)
G1_TOKEN="Bearer eyJhbGciOiJFUzI1NiIsImtpZCI6InRlc3Qta2lkIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxNDAwMDA1MTEiLCJqdGkiOiJ0ZXN0LWp0aSIsImlzcyI6Imh0dHBzOi8vYXV0aC56ZXRpLmNvbS8iLCJhdWQiOlsiaHR0cHM6Ly9hcGkuemV0aS5jb20iXSwiZXhwIjoxNzc4MDU2OTkzLCJpYXQiOjE3NzgwNTYzOTMsImF1dGhfdGltZSI6MTc3ODA1NjM5MywibmJmIjoxNzc4MDU2MzkzfQ.dummysig"
run_case "G1: 정상 ES256 → jwt.alg=ES256" \
    "{\"http_authorization\": \"$G1_TOKEN\"}" \
    "ES256" \
    '.docs[0].doc._source.jwt.alg'
run_case "G1: 정상 ES256 → jwt.sub=140000511" \
    "{\"http_authorization\": \"$G1_TOKEN\"}" \
    "140000511" \
    '.docs[0].doc._source.jwt.sub'
run_case "G1: 정상 ES256 → jwt.exp=1778056993 (long)" \
    "{\"http_authorization\": \"$G1_TOKEN\"}" \
    "1778056993" \
    '.docs[0].doc._source.jwt.exp'
run_case "G1: 정상 ES256 → http_authorization 제거됨" \
    "{\"http_authorization\": \"$G1_TOKEN\"}" \
    "null" \
    '.docs[0].doc._source.http_authorization'
run_case "G1: 정상 ES256 → jwt.decode_error null" \
    "{\"http_authorization\": \"$G1_TOKEN\"}" \
    "null" \
    '.docs[0].doc._source.jwt.decode_error'

# === G2: Authorization 헤더 없음 ===
run_case "G2: 헤더 없음 → jwt 객체 없음 (null)" \
    "{}" \
    "null" \
    '.docs[0].doc._source.jwt'

# === G3: Bearer garbage (구조 위반) ===
run_case "G3: Bearer garbage → jwt.decode_error 박힘" \
    "{\"http_authorization\": \"Bearer garbage\"}" \
    "true" \
    '.docs[0].doc._source.jwt.decode_error != null'

# === G4: HS256 위조 토큰 (alg confusion attack 시뮬) ===
# header: {"alg":"HS256","typ":"JWT"}
# payload: {"sub":"140000511"}
G4_TOKEN="Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxNDAwMDA1MTEifQ.dummysig"
run_case "G4: HS256 위조 → jwt.alg=HS256 정상 추출" \
    "{\"http_authorization\": \"$G4_TOKEN\"}" \
    "HS256" \
    '.docs[0].doc._source.jwt.alg'

echo ""
echo "=== 결과: PASS=$PASS / FAIL=$FAIL ==="
[ $FAIL -eq 0 ] && exit 0 || exit 1
