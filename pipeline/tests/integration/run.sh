#!/usr/bin/env bash
# P-02 통합·복구 시험: 일회용 MySQL 8.0 · Redis 7 · Elasticsearch 8.19(single-node) + relay/indexer 이미지.
#
#   PYTHON=pipeline/.venv/bin/python pipeline/tests/integration/run.sh [pytest 인자...]
#
# - 모든 컨테이너·네트워크·이미지 이름은 p02test-<run> 접두사. 종료 시(trap) 항상 삭제한다.
# - 호스트 포트는 127.0.0.1 임시 포트에만 bind한다. 실행 중인 다른 stack(zetty-*)은 건드리지 않는다.
# - 비밀번호는 실행마다 새로 만들고 임시 디렉터리 파일에만 둔다(출력하지 않음, 종료 시 삭제).
# - 환경 변수: PYTHON(기본 pipeline/.venv/bin/python), P02_TMP(임시 디렉터리 부모), P02_REPORT(결과 JSON 경로)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-pipeline/.venv/bin/python}"
ES_IMAGE="docker.elastic.co/elasticsearch/elasticsearch:8.19.14"
MYSQL_IMAGE="mysql:8.0"
REDIS_IMAGE="redis:7-alpine"
K6_IMAGE="grafana/k6:0.53.0"

RUN_ID="p02test-$(date +%Y%m%d%H%M%S)-$$"
NET="${RUN_ID}-net"
MYSQL_C="${RUN_ID}-mysql"
REDIS_C="${RUN_ID}-redis"
ES_C="${RUN_ID}-es"
RELAY_IMAGE="p02test-relay:${RUN_ID#p02test-}"
INDEXER_IMAGE="p02test-indexer:${RUN_ID#p02test-}"
TMP_PARENT="${P02_TMP:-${TMPDIR:-/tmp}}"
SECRETS="$(mktemp -d "${TMP_PARENT%/}/p02test.XXXXXX")"
REPORT="${P02_REPORT:-${SECRETS}/report.json}"

log() { printf '[run.sh] %s\n' "$*"; }

cleanup() {
  local rc=$?
  set +e
  log "cleanup ${RUN_ID}"
  local ids
  ids="$(docker ps -aq --filter "name=^/${RUN_ID}-")"
  [ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  docker image rm "$RELAY_IMAGE" "$INDEXER_IMAGE" >/dev/null 2>&1
  if [ "$REPORT" = "${SECRETS}/report.json" ] && [ -f "$REPORT" ]; then cat "$REPORT"; fi
  rm -rf "$SECRETS"
  log "remaining containers with prefix: $(docker ps -aq --filter "name=^/${RUN_ID}-" | wc -l | tr -d ' ')"
  exit $rc
}
trap cleanup EXIT INT TERM

wait_for_load_test() {
  while [ -n "$(docker ps -q --filter "ancestor=${K6_IMAGE}")" ]; do
    log "k6 load test running; waiting"
    sleep 20
  done
}

hostport() { docker port "$1" "$2" | head -n1; }

# ---------------------------------------------------------------- secrets (실행마다 생성)
chmod 755 "$SECRETS"
for name in mysql_root mysql_relay mysql_indexer mysql_ops redis_admin redis_relay redis_indexer; do
  openssl rand -hex 24 > "${SECRETS}/${name}"
done
printf 'requirepass %s\nappendonly no\nsave ""\n' "$(cat "${SECRETS}/redis_admin")" > "${SECRETS}/redis.conf"
chmod 644 "${SECRETS}"/*   # 컨테이너의 비root 사용자(uid 10001, mysql, redis)가 읽을 수 있게

# ---------------------------------------------------------------- images
log "build images"
docker build -q -f pipeline/relay/Dockerfile -t "$RELAY_IMAGE" . >/dev/null
docker build -q -f pipeline/indexer/Dockerfile -t "$INDEXER_IMAGE" . >/dev/null

# ---------------------------------------------------------------- infra
wait_for_load_test
docker network create "$NET" >/dev/null

log "start mysql"
docker run -d --name "$MYSQL_C" --network "$NET" --memory 1g \
  -p 127.0.0.1::3306 -v "${SECRETS}:/run/p02:ro" \
  -e MYSQL_ROOT_PASSWORD_FILE=/run/p02/mysql_root -e MYSQL_DATABASE=zeti_db \
  "$MYSQL_IMAGE" --innodb-buffer-pool-size=128M --skip-log-bin >/dev/null

log "start redis"
docker run -d --name "$REDIS_C" --network "$NET" --memory 256m \
  -p 127.0.0.1::6379 -v "${SECRETS}:/run/p02:ro" \
  "$REDIS_IMAGE" redis-server /run/p02/redis.conf >/dev/null

wait_for_load_test
log "start elasticsearch (heap 512m)"
docker run -d --name "$ES_C" --network "$NET" --memory 1536m \
  -p 127.0.0.1::9200 \
  -e discovery.type=single-node -e xpack.security.enabled=false -e xpack.ml.enabled=false \
  -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
  -e cluster.routing.allocation.disk.threshold_enabled=false \
  -e action.destructive_requires_name=false \
  "$ES_IMAGE" >/dev/null

export P02_RUN_ID="$RUN_ID" P02_NET="$NET" P02_SECRETS="$SECRETS" P02_REPORT="$REPORT"
export P02_RELAY_IMAGE="$RELAY_IMAGE" P02_INDEXER_IMAGE="$INDEXER_IMAGE"
export P02_MYSQL_CONTAINER="$MYSQL_C" P02_REDIS_CONTAINER="$REDIS_C" P02_ES_CONTAINER="$ES_C"
export P02_MYSQL_HOSTPORT="$(hostport "$MYSQL_C" 3306)"
export P02_REDIS_HOSTPORT="$(hostport "$REDIS_C" 6379)"
export P02_ES_HOSTPORT="$(hostport "$ES_C" 9200)"
export PYTHONDONTWRITEBYTECODE=1

log "run pytest (images: $RELAY_IMAGE, $INDEXER_IMAGE)"
set +e
"$PYTHON" -m pytest pipeline/tests/integration -p no:cacheprovider -v -s "$@"
rc=$?
set -e
log "pytest exit=$rc report=$REPORT"
exit $rc
