#!/bin/bash
# Phase 0.5-7 — Filebeat 실 트래픽 JWT 분해 검증용 cURL
#
# 실행: priv-web-2a SSM 에서 `bash scripts/test-jwt-curl.sh`
# 목적: 가짜 JWT (sub=140000511, ext.LSID=d8fa-test) 박힌 cURL 을 nginx /api/* 로 흘려서
#       uba.log → Filebeat processor → ES 색인 doc 의 jwt 가 object 로 분해되는지 검증.
#
# paste 깨짐 회피용 — SSM bracketed paste 가 들여쓰기 추가하는 환경에서
# 한 줄짜리 long-string 변수가 wrap 되며 newline 박히는 문제를 회피.
#
# 검증은 ELK SSM 에서 별도 (scripts/verify-jwt-es.sh 참조).

set -e

HEADER_JSON='{"alg":"ES256","kid":"test-2026-05-15","typ":"JWT"}'
PAYLOAD_JSON='{"sub":"140000511","jti":"test-jti-001","iat":1778056393,"exp":1778099593,"iss":"https://auth.zeti.com/","aud":["https://api.zeti.com"],"client_id":"zeti-web","scp":["openid","core"],"acr":"aal1","amr":["pwd"],"ext":{"LSID":"d8fa308d-test","fiat":1778056393,"v":2}}'

HEADER=$(printf '%s' "$HEADER_JSON"  | base64 -w0 | tr -d '=' | tr '+/' '-_')
PAYLOAD=$(printf '%s' "$PAYLOAD_JSON" | base64 -w0 | tr -d '=' | tr '+/' '-_')
SIG='fakesignaturefordecodetestonly'
TOKEN="${HEADER}.${PAYLOAD}.${SIG}"

echo "=== TOKEN sanity check ==="
echo "length        : ${#TOKEN} (정상 ~330~370 안팎)"
echo "dot count     : $(printf '%s' "$TOKEN" | tr -cd '.' | wc -c) (정상 2)"
echo "newline count : $(printf '%s' "$TOKEN" | tr -cd '\n' | wc -c) (정상 0)"
echo "first 50 char : ${TOKEN:0:50}"
echo ""

echo "=== cURL → nginx /api/users/me ==="
curl -sk -o /dev/null -w 'HTTP=%{http_code}\n' \
  -H "Authorization: Bearer ${TOKEN}" \
  http://localhost/api/users/me

echo ""
echo "=== uba.log 마지막 1줄 (Bearer 박힌 한 줄 인지 확인) ==="
sudo tail -n 1 /var/log/nginx/uba.log
