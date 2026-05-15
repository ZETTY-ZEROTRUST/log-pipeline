#!/bin/bash
# Phase 0.5-1 — nginx-pep uba.conf 통합본 배포 + 검증
#
# 실행: priv-web-2a/2b 양쪽 SSM 에서 한 번씩 `bash scripts/deploy-nginx-pep.sh`
# 전제: 사용자가 이미 git pull 로 최신 main 받음.
#
# 흐름:
#   1. 백업: /etc/nginx/conf.d/uba.conf → uba.conf.bak.YYYYMMDD-HHMM
#   2. 배포: nginx-pep/uba.conf → /etc/nginx/conf.d/uba.conf
#   3. nginx -t (syntax 검증). 실패 시 자동 복구 + exit.
#   4. nginx -s reload (무중단 적용)
#   5. cURL 검증 4건: / + /healthz + /api/users/me (no token) + /api/users/me (가짜 JWT)
#   6. uba.log 마지막 5줄 (JSON 포맷 유지 확인)
#
# 실패 시 수동 복구:
#   sudo cp /etc/nginx/conf.d/uba.conf.bak.* /etc/nginx/conf.d/uba.conf
#   sudo nginx -t && sudo nginx -s reload

set -e

BACKUP_SUFFIX=$(date +%Y%m%d-%H%M)
CONF_LIVE="/etc/nginx/conf.d/uba.conf"
CONF_BAK="${CONF_LIVE}.bak.${BACKUP_SUFFIX}"
CONF_NEW="$(dirname "$0")/../nginx-pep/uba.conf"

echo "=== [1] 머신 식별 ==="
hostname
ip -4 addr show ens5 | grep -oP 'inet \K[\d.]+'

echo ""
echo "=== [2] 백업 ==="
echo "현재 conf: ${CONF_LIVE}"
sudo cp "${CONF_LIVE}" "${CONF_BAK}"
echo "백업: ${CONF_BAK}"

echo ""
echo "=== [3] 배포 ==="
sudo cp "${CONF_NEW}" "${CONF_LIVE}"
echo "diff (백업 vs 신규):"
sudo diff -u "${CONF_BAK}" "${CONF_LIVE}" | head -50 || echo "(diff 출력 끝)"

echo ""
echo "=== [4] nginx -t (syntax 검증) ==="
if ! sudo nginx -t 2>&1; then
  echo ""
  echo "★ syntax 실패 — 자동 복구 진행"
  sudo cp "${CONF_BAK}" "${CONF_LIVE}"
  sudo nginx -t
  echo "복구 완료. nginx reload 안 함."
  exit 1
fi

echo ""
echo "=== [5] nginx -s reload (무중단 적용) ==="
sudo nginx -s reload
sleep 1
echo "active workers:"
ps aux | grep -E "[n]ginx" | head -5

echo ""
echo "=== [6] cURL 검증 4건 ==="
echo "--- (a) / → 'nginx-pep running' (머신 분기 X) ---"
curl -s -o /dev/null -w 'HTTP=%{http_code} body=' http://localhost/
echo "$(curl -s http://localhost/)"

echo "--- (b) /healthz → priv-app /healthz proxy (양 AZ round-robin) ---"
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "  try ${i}: HTTP=%{http_code} time=%{time_total}s\n" http://localhost/healthz
done

echo "--- (c) /api/users/me (Auth 헤더 없음 → 401 또는 400 기대) ---"
curl -s -o /dev/null -w 'HTTP=%{http_code}\n' http://localhost/api/users/me

echo "--- (d) /api/users/me (가짜 JWT → 401 — KMS 서명 검증 거부) ---"
HEADER=$(printf '%s' '{"alg":"ES256","typ":"JWT"}' | base64 -w0 | tr -d '=' | tr '+/' '-_')
PAYLOAD=$(printf '%s' '{"sub":"deploy-check","jti":"dc1","iat":1778056393,"exp":1778099593,"ext":{"LSID":"deploy-check-lsid"}}' | base64 -w0 | tr -d '=' | tr '+/' '-_')
TOKEN="${HEADER}.${PAYLOAD}.fakesig"
curl -s -o /dev/null -w 'HTTP=%{http_code}\n' -H "Authorization: Bearer ${TOKEN}" http://localhost/api/users/me

echo ""
echo "=== [7] uba.log 마지막 5줄 (JSON 포맷 유지 + 통합본의 cURL 흔적) ==="
sudo tail -n 5 /var/log/nginx/uba.log

echo ""
echo "=== 배포 완료 ==="
echo "롤백 필요시:"
echo "  sudo cp ${CONF_BAK} ${CONF_LIVE} && sudo nginx -t && sudo nginx -s reload"
