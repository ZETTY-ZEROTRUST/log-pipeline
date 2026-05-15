#!/bin/bash
# Phase 0.5-7 — JWT 분해 ES 검증
#
# 실행: ELK SSM 에서 `bash scripts/verify-jwt-es.sh`
# 전제: scripts/test-jwt-curl.sh 가 priv-web-2a 에서 5분 이내 실행됨.
#
# 4 단계 검증:
#   [1] sub=140000511 검색 — jwt 가 object 로 분해됐는지 핵심 (hit ≥ 1)
#   [2] jwt_decode_error 발생 doc 수 (5분 내, 0 기대)
#   [3] (실패 시) decode_error 박힌 doc 의 _source 전체
#   [4] 2a/2b host 별 doc 분포 (양쪽 색인 동등성)

set -e

ES_PASS='Qx74mrJEwWv3E++6F-AY'
ES_HOST='https://localhost:9200'

q() {
  curl -sk -u "elastic:${ES_PASS}" "${ES_HOST}/$1" \
    -H 'Content-Type: application/json' -d "$2" | python3 -m json.tool
}

echo "=== [1] sub=140000511 검색 (G-2 통과 핵심) ==="
q 'filebeat-*/_search?size=3&sort=@timestamp:desc' '{
  "query": {"term": {"jwt.sub.keyword": "140000511"}},
  "_source": ["@timestamp","host.name","jwt","client_ip","ip_class","is_nat_whitelisted","jwt_decode_error"]
}'

echo ""
echo "=== [2] decode_error 발생 doc 수 (5분 내, 0 기대) ==="
q 'filebeat-*/_search?size=0' '{
  "query": {
    "bool": {"must": [
      {"range": {"@timestamp": {"gte": "now-5m"}}},
      {"exists": {"field": "jwt_decode_error"}}
    ]}
  }
}'

echo ""
echo "=== [3] decode_error doc 의 _source (실패 진단용, 10분 내) ==="
q 'filebeat-*/_search?size=2&sort=@timestamp:desc' '{
  "query": {
    "bool": {"must": [
      {"range": {"@timestamp": {"gte": "now-10m"}}},
      {"exists": {"field": "jwt_decode_error"}}
    ]}
  }
}'

echo ""
echo "=== [4] 최근 5분 host 별 doc 분포 (2a/2b 색인 동등성) ==="
q 'filebeat-*/_search?size=0' '{
  "query": {"range": {"@timestamp": {"gte": "now-5m"}}},
  "aggs": {"by_host": {"terms": {"field": "host.name", "size": 5}}}
}'
