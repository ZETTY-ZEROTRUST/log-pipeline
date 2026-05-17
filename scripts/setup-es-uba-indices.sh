#!/usr/bin/env bash
#
# setup-es-uba-indices.sh — UBA 출력 인덱스 템플릿을 ES 에 PUT
#
# uba-events / uba-risk-scores / uba-alerts / uba-intelligence / uba-user-profiles
# 5개 composable index template 을 등록한다. setup-es-ingest.sh 는 filebeat-jwt
# 템플릿만 다루므로, Phase 1/2/3 가 쓰는 UBA 출력 인덱스 템플릿은 이 스크립트가 담당한다.
#
# 멱등(idempotent) — 여러 번 실행해도 안전하다. 인덱스 템플릿 PUT 은 기존 걸
# 덮어쓸 뿐, 이미 색인된 데이터에는 영향이 없다 (재색인 불필요).
#
# 실행 (ELK SSM):
#   cd /tmp/log-pipeline && git pull
#   bash scripts/setup-es-uba-indices.sh
#
set -euo pipefail

ES_HOST="${ES_HOST:-https://10.0.41.10:9200}"
ES_USER="${ES_USER:-elastic}"
ES_PASS="${ES_PASS:-Qx74mrJEwWv3E++6F-AY}"

MAP_DIR="$(cd "$(dirname "$0")/../es-mappings" && pwd)"

echo "=== UBA 인덱스 템플릿 PUT ==="
echo "  ES_HOST: $ES_HOST"
echo "  매핑 디렉토리: $MAP_DIR"
echo

# 템플릿명 = 파일명(.json 제외). index_patterns 는 각 JSON 안에 정의돼 있다.
for name in uba-events uba-risk-scores uba-alerts uba-intelligence uba-user-profiles; do
  file="$MAP_DIR/${name}.json"
  if [ ! -f "$file" ]; then
    echo "  [SKIP] ${name}.json 없음"
    continue
  fi
  echo -n "  PUT _index_template/${name} ... "
  code=$(curl -sk -o /tmp/uba-tmpl-resp.json -w "%{http_code}" \
    -u "$ES_USER:$ES_PASS" -X PUT "$ES_HOST/_index_template/${name}" \
    -H 'Content-Type: application/json' --data-binary "@${file}")
  if [ "$code" = "200" ]; then
    echo "OK"
  else
    echo "실패 (HTTP $code)"
    cat /tmp/uba-tmpl-resp.json
    echo
    exit 1
  fi
done

echo
echo "=== 등록 확인 ==="
curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cat/templates/uba-*?v"
echo
echo "완료. (인덱스 실체는 Phase 1/2 가 첫 doc 을 쓸 때 이 템플릿대로 생성됨)"
