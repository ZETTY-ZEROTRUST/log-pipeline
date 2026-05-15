#!/bin/bash
# Phase 0.5-5 매핑 재설정 (2026-05-15)
#
# 배경:
#   4/19 Filebeat 첫 가동 시 default filebeat-8.19.14 template로 데이터스트림 생성.
#   ALB 헬스체크의 `jwt:""` 빈 문자열로 dynamic mapping 굳어서 jwt=keyword.
#   본인 5/15 PUT한 filebeat-jwt template는 priority/data_stream 누락으로 적용 안 됨.
#   결과: cURL JWT 박힌 doc가 mapping conflict로 ES 거부됨 (decode_error도 안 박힘).
#
# 해결:
#   1. 데이터스트림 + filebeat-8.19.14 자체 template 모두 삭제
#   2. priority 500 + data_stream:{} 추가한 filebeat-jwt template 재 PUT
#   3. 사용자가 priv-web-2a에서 test-jwt-curl.sh 재실행 → 새 데이터스트림 생성됨
#   4. verify-jwt-es.sh 로 jwt object 정상 분해 확인
#
# 사용: ELK SSM 안에서 `bash scripts/reset-es-mapping.sh`
#
# Destructive: filebeat-* 데이터스트림 77K doc 통째 삭제. 시연용이라 가치 0.

set -e

ES_PASS='Qx74mrJEwWv3E++6F-AY'
ES_HOST='https://localhost:9200'
AUTH="-u elastic:${ES_PASS}"

curl_es() {
  curl -sk ${AUTH} "$@"
}

echo "=== [A] 작업 전 상태 ==="
echo "--- filebeat-jwt template (현재) ---"
curl_es "${ES_HOST}/_index_template/filebeat-jwt" | python3 -m json.tool 2>/dev/null | head -20 || echo "(존재 안 함)"
echo "--- filebeat-* 인덱스 ---"
curl_es "${ES_HOST}/_cat/indices/filebeat-*?v"
echo "--- 데이터스트림 ---"
curl_es "${ES_HOST}/_cat/data_streams/filebeat-*"

echo ""
echo "=== [B] 삭제 — 데이터스트림 + Filebeat 자체 template ==="

echo "--- filebeat-8.19.14 데이터스트림 삭제 ---"
curl_es -X DELETE "${ES_HOST}/_data_stream/filebeat-8.19.14"; echo ""

echo "--- filebeat-8.19.14 composable template 삭제 ---"
curl_es -X DELETE "${ES_HOST}/_index_template/filebeat-8.19.14"; echo ""

echo "--- filebeat-8.19.14 legacy template 삭제 (fallback) ---"
curl_es -X DELETE "${ES_HOST}/_template/filebeat-8.19.14"; echo ""

echo "--- 우리 filebeat-jwt template 도 삭제 (재 PUT을 위해) ---"
curl_es -X DELETE "${ES_HOST}/_index_template/filebeat-jwt"; echo ""

echo ""
echo "=== [C] 보강된 filebeat-jwt template 재 PUT (priority 500 + data_stream + ext nested) ==="
PUT_BODY=$(cat "$(dirname "$0")/../es-mappings/filebeat-jwt-template.json")
curl_es -X PUT "${ES_HOST}/_index_template/filebeat-jwt" \
  -H 'Content-Type: application/json' \
  -d "${PUT_BODY}"
echo ""

echo ""
echo "=== [D] PUT 확인 ==="
curl_es "${ES_HOST}/_index_template/filebeat-jwt" \
  | python3 -c "import json,sys; t=json.load(sys.stdin)['index_templates'][0]; print('name:', t['name']); print('priority:', t['index_template'].get('priority','MISSING')); print('data_stream:', t['index_template'].get('data_stream','MISSING')); print('jwt props:', list(t['index_template']['template']['mappings']['properties'].get('jwt',{}).get('properties',{}).keys()))"

echo ""
echo "=== [E] 작업 후 상태 ==="
echo "--- filebeat-* 인덱스 (비어있어야 정상) ---"
curl_es "${ES_HOST}/_cat/indices/filebeat-*?v"
echo "--- 데이터스트림 (비어있어야 정상) ---"
curl_es "${ES_HOST}/_cat/data_streams/filebeat-*"

echo ""
echo "=== 다음 단계 ==="
echo "1. priv-web-2a SSM 에서:  cd /tmp/log-pipeline && git pull && bash scripts/test-jwt-curl.sh"
echo "2. 다시 ELK SSM 에서:      sleep 10 && bash scripts/verify-jwt-es.sh"
echo "3. [1] jwt.sub.keyword=140000511 hit ≥ 1 이면 G-2 통과"
