#!/usr/bin/env python3
"""
ZETI Phase 1 — baseline doc seed (정상 트래픽 시뮬, _bulk 직접 주입)

ES filebeat-* 데이터스트림에 가짜 정상 트래픽 doc 을 _bulk 로 박는다.
24h 트래픽 시뮬레이터 가동 누적 시계 없이 baseline 즉시 형성.

v2 보강 (2026-05-16, Codex advisory + attack-simulation v2 / v12 정합):
    - kid → alias/jwt-signing-key-external (backend auth/api-server 진실)
    - --hours default 72 (cumulative_exfil 3일 EMA 분량 확보)
    - URI 응답 크기 gauss 현실화 (addresses 2400±720 / orders 1800±540 등)
    - URI 선택 가중치 (균등 random X — products/users-me 중심)
    - jti 세션 버스트 (한 세션 = 한 토큰, 5분 내 요청 클러스터). 단일 cgnat_kr IP 안의 jti 재사용은
      v12 token_replay(ip_class 교차) 기준으로 정상 → seed 는 token_replay 0
    - user_agent 필드 추가 (ua_set 입력 — seed 는 _bulk 라 nginx cascade 무관)
    - 일중 패턴 (diurnal_weight) — request_burst z-score 가 의미를 갖게
    - provenance 메타 (zeti_seed.*) + ip_class_source=seed_bulk (정직한 출처 표기)
    - --scenario {normal,s7-household,s7-cafe,all} (S7 은 보류 트랙 — 옵션만 보존)

실행 (ELK SSM 안에서):
    cd /tmp/log-pipeline && git pull
    python3 scripts/seed-baseline.py                       # normal 72h baseline
    python3 scripts/seed-baseline.py --scenario all         # normal + S7 2종
    python3 scripts/seed-baseline.py --hours 48 --count 6000

전제:
    - ES (10.0.41.10:9200) 도달 가능
    - filebeat-jwt template (priority 500 + data_stream + jwt 17 nested) 적용됨 (Phase 0.5 ✅)
    - 매핑은 dynamic:false → user_agent / zeti_seed 는 _source 보존만 (색인 X, aggregator 가 _source 로 읽음)

색인 후 검증:
    - filebeat-* 인덱스에 doc count 증가
    - jwt.sub 다양성 = USERS 수, jwt.kid 단일값 (alias/jwt-signing-key-external)
    - ip_class=cgnat_kr 분포
    - 시간 분포: 72h 범위 + 일중 패턴 (새벽 낮고 낮 높음)

의존성: Python 3.7+ stdlib 만 (requests 없음 — urllib + ssl)
"""

import argparse
import base64
import json
import random
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

ES_HOST = "https://10.0.41.10:9200"
ES_USER = "elastic"
ES_PASS = "Qx74mrJEwWv3E++6F-AY"
TARGET_DATASTREAM = "filebeat-8.19.14"

# backend auth-server/api-server + forge_token.py 가 모두 쓰는 실 KMS alias.
# (AGENTS.md 의 {uuid}-{yyyy-mm-dd} 스펙은 미래형 — 현 구현은 alias 그대로 kid 사용)
KID = "alias/jwt-signing-key-external"

KST = timezone(timedelta(hours=9))
JWT_TTL_SEC = 600                  # 정상 토큰 TTL — backend auth-server application-prod.yml: expiration: 600
DEFAULT_DOCS_PER_HOUR = 120        # normal scenario: 시간당 doc 수 (--count 미지정 시)
# 세션 버스트 모델 — 정상 트래픽은 "로그인→몇 분 집중 클릭→유휴" 형태. doc 을 72h 에
# 고르게 흩뿌리면 5분 윈도우당 ~1건이라 request_burst baseline 이 무너진다.
SESSION_SPAN_SEC = 290             # 한 세션의 요청이 퍼지는 시간 (TTL 600 안)
SESSION_REQ_MEAN = 15              # 세션당 요청 수 평균
SESSION_REQ_STD = 6
MIN_SESSION_REQ = 3
MAX_SESSION_REQ = 40
S7_HOUSEHOLD_COUNT = 480           # 단일 IP × 4 user
S7_CAFE_COUNT = 1500               # 단일 IP × 50 user

# ────────────────────────────────────────────────────────────────────────────
# 정상 사용자 풀 (8명) — KT (AS4766) 인터넷 IP, 각자 고유 LSID
# sub 140000511~518: attack victim range(140000002~140100000) 안 — 시간/IP 차원으로
# 분리 (seed=과거 72h/KT IP vs attack=현재/cloud IP). sub 분리는 사용자 결정으로 보류.
# ────────────────────────────────────────────────────────────────────────────
NORMAL_USERS = [
    {"sub": "140000511", "ip": "118.235.82.230", "lsid": "lsid-user-1-d8fa", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"sub": "140000512", "ip": "211.234.105.42", "lsid": "lsid-user-2-3a7c", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"},
    {"sub": "140000513", "ip": "175.223.18.99",  "lsid": "lsid-user-3-9e21", "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"},
    {"sub": "140000514", "ip": "121.140.65.10",  "lsid": "lsid-user-4-44ab", "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"},
    {"sub": "140000515", "ip": "59.6.87.221",    "lsid": "lsid-user-5-7e89", "ua": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"},
    {"sub": "140000516", "ip": "210.94.115.7",   "lsid": "lsid-user-6-c032", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/120.0"},
    {"sub": "140000517", "ip": "112.169.23.88",  "lsid": "lsid-user-7-91ef", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 Chrome/120.0"},
    {"sub": "140000518", "ip": "1.234.55.106",   "lsid": "lsid-user-8-2b65", "ua": "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"},
]

# S7 전용 UA 풀 — 단일 IP 안에서 UA 다양성을 만들어 NAT 판정 근거를 남긴다.
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/120.0",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
]

# URI 풀 — weight(선택 확률) / method / bytes(gauss mean,std) / status
# 정상 사용자는 둘러보기 중심: products·users/me 가 무겁고 addresses 는 가볍다.
# (addresses 를 가볍게 둬야 attack-simulation 의 addresses 100% 패턴이 baseline 대비 도드라진다)
NORMAL_URIS = [
    {"uri": "/api/users/me",           "method": "GET",  "weight": 20, "bytes": (150, 45),   "status": 200},
    {"uri": "/api/products",           "method": "GET",  "weight": 25, "bytes": (2100, 630), "status": 200},
    {"uri": "/api/products/{id}",      "method": "GET",  "weight": 20, "bytes": (400, 120),  "status": 200},
    {"uri": "/api/cart",               "method": "GET",  "weight": 12, "bytes": (220, 66),   "status": 200},
    {"uri": "/api/cart/items",         "method": "POST", "weight": 6,  "bytes": (50, 15),    "status": 200},
    {"uri": "/api/orders",             "method": "GET",  "weight": 8,  "bytes": (800, 240),  "status": 200},
    {"uri": "/api/orders/{userId}",    "method": "GET",  "weight": 5,  "bytes": (1800, 540), "status": 200},
    {"uri": "/api/addresses/{userId}", "method": "GET",  "weight": 2,  "bytes": (2400, 720), "status": 200},
    {"uri": "/auth/refresh",           "method": "POST", "weight": 2,  "bytes": (280, 84),   "status": 200},
]
# 3% 노이즈 — 정상 사용자도 가끔 만료 토큰/404 발생
NOISE_URIS = [
    {"uri": "/api/products/{id}",      "method": "GET",  "weight": 1,  "bytes": (80, 20),    "status": 404},
    {"uri": "/api/users/me",           "method": "GET",  "weight": 1,  "bytes": (0, 0),      "status": 401},
]

PRIV_WEB_HOSTS = ["ip-10-0-11-31", "ip-10-0-12-162"]
ALB_NGINX_IPS = ["10.0.1.109", "10.0.2.24"]   # nginx 가 본 직접 클라이언트 (ALB IP)

S7_HOUSEHOLD_IP = "121.135.40.77"   # 단일 가정 NAT (KT) — user 4명
S7_CAFE_IP = "175.197.50.12"        # 단일 카페 NAT (KT) — user 50명


# ────────────────────────────────────────────────────────────────────────────
# 시간 분포 — 일중 패턴
# ────────────────────────────────────────────────────────────────────────────
def diurnal_weight(hour_kst: int) -> float:
    """시각(KST 0~23)에 대한 상대 트래픽 가중치를 0.0 ~ 1.0 으로 반환.

    sample_timestamp() 가 rejection sampling 으로 이 값을 쓴다:
    diurnal_weight 가 클수록 그 시간대 doc 이 채택될 확률이 높다.
    1.0 이면 항상 채택, 0.0 이면 절대 채택 안 함.
    """
    # 한국 이커머스 일중 곡선 (리서치 기반):
    #   - 06시부터 상승, 13시 오후 피크(점심대), 04시 바닥
    #   - 19시(7pm) 전역 피크 — 저녁(18~21시)이 오후보다 강하다 ("저녁 > 낮" 골격)
    # 인덱스 = KST 시각(0~23). request_burst z-score 가 '평소 대비 급증'으로
    # 의미를 갖도록, normal baseline 자체에 이 일중 리듬을 새긴다.
    HOURLY_WEIGHT = (
        0.30, 0.18, 0.10, 0.06, 0.05, 0.07,   # 00~05  심야 → 바닥(04시)
        0.15, 0.30, 0.45, 0.55, 0.62, 0.70,   # 06~11  상승 (출근·오전)
        0.80, 0.85, 0.82, 0.70, 0.66, 0.70,   # 12~17  점심 피크(13시) 후 소강
        0.82, 1.00, 0.98, 0.90, 0.72, 0.50,   # 18~23  저녁 피크(19시=7pm)
    )
    return HOURLY_WEIGHT[hour_kst % 24]


def sample_timestamp(start: datetime, hours: int) -> datetime:
    """diurnal_weight 기반 rejection sampling 으로 doc 시각 1개를 추출."""
    while True:
        offset = random.uniform(0, hours * 3600)
        ts = start + timedelta(seconds=offset)
        if random.random() < diurnal_weight(ts.astimezone(KST).hour):
            return ts


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
def make_jwt_object(user, jti, iat):
    """Filebeat processor 가 분해한 jwt object 형태 그대로.

    jti / iat 는 세션 단위로 정해진다 — 한 세션 = 한 토큰. exp = iat + 600.
    세션 길이(SESSION_SPAN_SEC)가 TTL(600)보다 짧으므로 세션 안의 모든 doc 은
    만료 전(token_status=valid) — 정상 baseline 에 expired 노이즈가 안 섞인다.
    """
    return {
        "alg": "ES256",
        "kid": KID,
        "typ": "JWT",
        "sub": user["sub"],
        "jti": jti,
        "iat": iat,
        "exp": iat + JWT_TTL_SEC,
        "auth_time": iat,
        "nbf": iat,
        "iss": "https://auth.zeti.com/",
        "aud": ["https://api.zeti.com"],
        "client_id": "zeti-web",
        "scp": ["openid", "core"],
        "acr": "aal1",
        "amr": ["pwd"],
        "ext": {
            "LSID": user["lsid"],
            "fiat": iat,
            "v": 2,
        },
    }


def make_doc(user, doc_ts, jti, iat, scenario, baseline_eligible):
    """nginx → Filebeat (JWT 분해) → asn-classify 처리 후 ES 색인되는 doc 형태."""
    pool = NORMAL_URIS if random.random() < 0.97 else NOISE_URIS
    entry = random.choices(pool, weights=[u["weight"] for u in pool], k=1)[0]
    uri = entry["uri"].replace("{userId}", user["sub"]).replace("{id}", str(random.randint(1, 9999)))
    mean, std = entry["bytes"]
    bytes_sent = max(0, int(random.gauss(mean, std))) if mean else 0
    response_time = round(max(0.001, random.gauss(0.05, 0.04)), 3)

    return {
        "@timestamp": doc_ts.isoformat().replace("+00:00", "Z"),
        "time": doc_ts.replace(microsecond=0).isoformat(),
        "ip": random.choice(ALB_NGINX_IPS),
        "x_forwarded_for": user["ip"],
        "client_ip": user["ip"],
        "user_agent": user["ua"],
        "ip_asn": "AS4766",
        "ip_org": "Korea Telecom",
        "ip_country": "KR",
        "ip_class": "cgnat_kr",
        "is_nat_whitelisted": True,
        "ip_class_source": "seed_bulk",   # 정직한 출처 — asn-classify pipeline 우회 (_bulk 직접 주입)
        "method": entry["method"],
        "uri": uri,
        "status": str(entry["status"]),
        "bytes_sent": str(bytes_sent),
        "response_time": str(response_time),
        "jwt": make_jwt_object(user, jti, iat),
        "host": {"name": random.choice(PRIV_WEB_HOSTS)},
        # provenance — 매핑 dynamic:false 라 색인 X, _source 보존만. aggregator 가 구분에 사용.
        "zeti_seed": {
            "source": "scenario_seed",
            "scenario": scenario,
            "baseline_eligible": baseline_eligible,
        },
        # 아래도 dynamic:false 라 _source 보존만 (색인 X)
        "agent": {"type": "filebeat", "version": "8.19.14"},
        "input": {"type": "log"},
        "log": {"file": {"path": "/var/log/nginx/uba.log"}},
        "ecs": {"version": "8.0.0"},
    }


def make_user(sub, ip):
    """S7 시나리오용 동적 사용자 — 단일 IP 안에서 UA 다양."""
    return {
        "sub": str(sub),
        "ip": ip,
        "lsid": f"lsid-{sub}-{uuid.uuid4().hex[:4]}",
        "ua": random.choice(UA_POOL),
    }


def make_session(user, sess_start, scenario, baseline_eligible):
    """한 세션 = 한 토큰(jti)으로 SESSION_SPAN_SEC 안에 N건 요청을 묶은 클러스터.

    ★ 정상 트래픽은 버스트다 — 로그인 후 몇 분 집중 클릭 → 유휴. doc 을 균등하게
    흩뿌리면 5분 윈도우당 ~1건이라 request_burst baseline 이 degenerate 해진다.
    세션 단위로 묶어야 "활성 윈도우 = 5~20건" 이라는 현실적 분포가 생긴다.
    """
    jti = str(uuid.uuid4())
    iat = int(sess_start.timestamp())
    n_req = max(MIN_SESSION_REQ,
                min(MAX_SESSION_REQ, int(random.gauss(SESSION_REQ_MEAN, SESSION_REQ_STD))))
    docs = []
    for _ in range(n_req):
        doc_ts = sess_start + timedelta(seconds=random.uniform(0, SESSION_SPAN_SEC))
        docs.append(make_doc(user, doc_ts, jti, iat, scenario, baseline_eligible))
    return docs


def build_scenario_docs(scenario, hours, normal_count, start):
    """선택된 scenario 의 doc 리스트 생성 (세션 버스트 모델).

    각 시나리오는 목표 doc 수에 도달할 때까지 세션을 반복 생성한다.
    """
    docs = []

    def _fill(users, target, scen, eligible):
        produced = 0
        while produced < target:
            user = random.choice(users)
            sess_start = sample_timestamp(start, hours)   # 세션 시작 = 일중 분포
            sess = make_session(user, sess_start, scen, eligible)
            docs.extend(sess)
            produced += len(sess)

    if scenario in ("normal", "all"):
        _fill(NORMAL_USERS, normal_count, "normal", True)

    if scenario in ("s7-household", "all"):
        # 단일 IP × user 4명 → unique_subs=4 (soft cap fallback 임계 5/10 미만 확인용)
        hh_users = [make_user(s, S7_HOUSEHOLD_IP) for s in range(140000700, 140000704)]
        _fill(hh_users, S7_HOUSEHOLD_COUNT, "s7-household", True)

    if scenario in ("s7-cafe", "all"):
        # 단일 IP × user 50명 → unique_subs=50. baseline_eligible=False:
        # 이 IP 를 baseline 에 누적하면 평균 unique_subs 가 커져 이후 공격 검출이 둔해진다.
        cafe_users = [make_user(s, S7_CAFE_IP) for s in range(140000800, 140000850)]
        _fill(cafe_users, S7_CAFE_COUNT, "s7-cafe", False)

    return docs


def es_bulk(docs, datastream=TARGET_DATASTREAM):
    """ES 데이터스트림은 op_type: create 만 허용."""
    lines = []
    for d in docs:
        lines.append(json.dumps({"create": {"_index": datastream}}))
        lines.append(json.dumps(d))
    body = "\n".join(lines) + "\n"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    auth = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req = urllib.request.Request(
        f"{ES_HOST}/_bulk",
        data=body.encode(),
        method="POST",
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Basic {auth}",
        }
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        raise


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ZETI Phase 1 baseline seed")
    parser.add_argument("--scenario", choices=["normal", "s7-household", "s7-cafe", "all"],
                        default="normal", help="시나리오 (default normal)")
    parser.add_argument("--count", type=int, default=None,
                        help=f"normal doc 수 (default = hours × {DEFAULT_DOCS_PER_HOUR})")
    parser.add_argument("--hours", type=int, default=72,
                        help="시간 윈도우 (default 72h — cumulative_exfil 3일 EMA)")
    parser.add_argument("--batch", type=int, default=500, help="_bulk 배치 크기 (default 500)")
    parser.add_argument("--seed", type=int, default=None, help="random seed (재현 가능 시)")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    normal_count = args.count if args.count is not None else args.hours * DEFAULT_DOCS_PER_HOUR
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=args.hours)

    print(f"=== ZETI Phase 1 baseline seed ===")
    print(f"  대상 데이터스트림: {TARGET_DATASTREAM}")
    print(f"  시나리오:          {args.scenario}")
    print(f"  시간 윈도우:       {start.isoformat()} ~ {now.isoformat()} ({args.hours}h)")
    print(f"  kid:               {KID}")
    if args.scenario in ("normal", "all"):
        print(f"  normal doc 수:     {normal_count} (user {len(NORMAL_USERS)}명)")
    if args.scenario in ("s7-household", "all"):
        print(f"  s7-household:      {S7_HOUSEHOLD_COUNT} (단일 IP × 4 user, baseline_eligible)")
    if args.scenario in ("s7-cafe", "all"):
        print(f"  s7-cafe:           {S7_CAFE_COUNT} (단일 IP × 50 user, baseline 제외)")
    print(f"  배치 크기:         {args.batch}")
    print()

    docs = build_scenario_docs(args.scenario, args.hours, normal_count, start)
    docs.sort(key=lambda d: d["@timestamp"])

    total = len(docs)
    inserted = 0
    errors = 0
    for i in range(0, total, args.batch):
        batch = docs[i:i + args.batch]
        result = es_bulk(batch)

        batch_err = 0
        if result.get("errors"):
            for item in result.get("items", []):
                if item.get("create", {}).get("error"):
                    batch_err += 1
                    if errors == 0:  # 첫 에러만 자세히
                        print(f"  ★ 첫 에러: {item['create']['error']}", file=sys.stderr)
        errors += batch_err
        inserted += len(batch) - batch_err

        print(f"  batch {i // args.batch + 1}: {len(batch) - batch_err}/{len(batch)} 성공, "
              f"누적 {inserted}/{total} (took {result.get('took', '?')}ms)")

    print()
    print(f"=== 완료 ===")
    print(f"  insert 성공: {inserted}")
    print(f"  insert 실패: {errors}")
    print()
    print(f"검증 명령 예시:")
    print(f"  curl -sk -u 'elastic:{ES_PASS}' '{ES_HOST}/filebeat-*/_count'")
    print(f"  curl -sk -u 'elastic:{ES_PASS}' '{ES_HOST}/filebeat-*/_search?size=0' \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"aggs\":{{\"by_sub\":{{\"terms\":{{\"field\":\"jwt.sub\",\"size\":60}}}},"
          f"\"by_kid\":{{\"terms\":{{\"field\":\"jwt.kid\"}}}},"
          f"\"by_ipclass\":{{\"terms\":{{\"field\":\"ip_class\"}}}}}}}}'")


if __name__ == "__main__":
    main()
