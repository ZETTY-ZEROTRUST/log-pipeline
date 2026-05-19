#!/usr/bin/env python3
"""
ZETI Phase 1 — baseline doc seed (정상 트래픽 시뮬, _bulk 직접 주입)

ES filebeat-* 데이터스트림에 가짜 정상 트래픽 doc 을 _bulk 로 박는다.
일주일치 트래픽 시뮬레이터 가동 누적 시계 없이 baseline 즉시 형성.

v3 보강 (2026-05-19, Track B — degenerate baseline 해소):
    - B1 IP 다양성: 유저당 IP 1~3개 + 일부 IP 를 2~3명이 공유(가정 NAT) →
      IP-윈도우 unique_subs 가 실분포 → ip_user_diversity baseline degeneracy 해소
    - B2 ASN 다양성: 5개 KR 캐리어 ASN 분산. ip_class 는 asn-map.yaml 기준 전부
      cgnat_kr 통일(한국 통신사 = 전부 CGNAT). ip_asn 은 LLM 컨텍스트·대시보드
      현실성용 — ip_aggregator 가 cgnat_kr 을 ASN 집계서 빼므로 asn_user_diversity
      엔 정상 트래픽 미반영 (detection 효과 아님)
    - B3 유저 200명: DB 실유저 범위(140000000~140000199)에서 활성 코호트 추출.
      DEFAULT_DOCS_PER_HOUR 700 으로 1인당 footprint 확보
    - B4 페르소나 + 요일 가중: light/normal/power 행동 차등 + dow_weight 주간 리듬
    - --hours default 168 (7일 — UBA 표준 baseline 윈도우, 요일 한 사이클)

v2 (2026-05-16): kid alias, 세션 버스트 모델, diurnal, provenance, --scenario.

실행 (ELK SSM 안에서):
    cd /tmp/log-pipeline && git pull
    python3 scripts/seed-baseline.py                       # normal 168h baseline
    python3 scripts/seed-baseline.py --scenario all         # normal + S7 2종
    python3 scripts/seed-baseline.py --hours 72 --count 50000

전제:
    - ES (10.0.41.10:9200) 도달 가능
    - filebeat-jwt template (priority 500 + data_stream + jwt 17 nested) 적용됨 (Phase 0.5 ✅)
    - 매핑은 dynamic:false → user_agent / zeti_seed 는 _source 보존만 (색인 X)

색인 후 검증:
    - filebeat-* 인덱스에 doc count 증가
    - jwt.sub 다양성 = NORMAL_USER_COUNT, jwt.kid 단일값
    - ip_class=cgnat_kr 단일 / ip_asn 5개 캐리어 분포
    - 시간 분포: 168h 범위 + 일중·요일 패턴

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
KID = "alias/jwt-signing-key-external"

KST = timezone(timedelta(hours=9))
JWT_TTL_SEC = 600                  # 정상 토큰 TTL — backend auth-server expiration: 600
DEFAULT_DOCS_PER_HOUR = 700        # v3: 120→700 — 유저 200명 × 1인당 footprint 확보
# 세션 버스트 모델 — 정상 트래픽은 "로그인→몇 분 집중 클릭→유휴" 형태.
SESSION_SPAN_SEC = 290             # 한 세션의 요청이 퍼지는 시간 (TTL 600 안)
MIN_SESSION_REQ = 3                # 세션당 요청 수 하한 (persona 무관 clamp)
MAX_SESSION_REQ = 40               # 세션당 요청 수 상한

# B3 — 정상 유저 활성 코호트: api-server/data.sql 이 적재한 DB 실유저
# (140000000~140000499) 중 앞 200명. 나머지 ~300명은 휴면 계정.
NORMAL_USER_COUNT = 200
NORMAL_SUB_START = 140000000

# B1 — 가정 NAT 비율: 이 확률로 2~3명이 primary IP 를 공유한다.
HOUSEHOLD_RATE = 0.35

S7_HOUSEHOLD_COUNT = 480           # 단일 IP × 4 user
S7_CAFE_COUNT = 1500               # 단일 IP × 50 user
S7_HOUSEHOLD_IP = "121.135.40.77"  # 단일 가정 NAT (KT)
S7_CAFE_IP = "175.197.50.12"       # 단일 카페 NAT (KT)


# ────────────────────────────────────────────────────────────────────────────
# B2 — 정상 한국 가정/모바일 회선
# asn-map.yaml 기준 한국 통신사 5개 — 전부 cgnat_kr (CGNAT). ip_class 는 cgnat_kr
# 통일이 정답. B2 가 흩는 ip_asn 은 LLM 컨텍스트·대시보드 현실성용 —
# ip_aggregator 가 cgnat_kr 을 ASN 집계서 제외하므로 asn_user_diversity 엔 안 들어감.
# ────────────────────────────────────────────────────────────────────────────
KR_CARRIERS = [
    {"asn": "AS4766",  "org": "Korea Telecom", "blocks": ["118.235", "211.234", "121.140"]},
    {"asn": "AS9318",  "org": "SK Broadband",  "blocks": ["175.223", "112.169"]},
    {"asn": "AS9644",  "org": "SK Telecom",    "blocks": ["223.38", "203.226"]},
    {"asn": "AS17858", "org": "LG U+",         "blocks": ["59.6", "211.36"]},
    {"asn": "AS3786",  "org": "LG DACOM",      "blocks": ["210.94", "1.234"]},
]


# ────────────────────────────────────────────────────────────────────────────
# B4 — 정상 유저 행동 페르소나
# 유저마다 light/normal/power 중 하나를 배정해 세션 빈도·세션당 요청수를 차등한다.
# 이 분산이 request_burst baseline 을 0/cap 이진에서 실분포로 입체화한다.
# ────────────────────────────────────────────────────────────────────────────
PERSONAS = {
    # population        : 200명 중 비율 (3개 합 = 1.0)
    # session_req_mean/std: 세션당 요청 수 (gauss) — MIN/MAX_SESSION_REQ(3~40) 안에서 clamp
    # activity          : 세션 생성 빈도 가중 (상대값 — 클수록 자주 로그인)
    #
    # light < normal < power 로 차등 — 이 분산이 request_burst baseline 을 입체화한다.
    # 스프레드는 ~5배(light 6 ↔ power 28)로만 — 너무 벌리면 정상 분포가 과하게
    # 퍼져 공격의 z-score 가 그 안에 묻힌다.
    "light":  {"population": 0.35, "session_req_mean": 6,  "session_req_std": 2, "activity": 0.6},
    "normal": {"population": 0.50, "session_req_mean": 14, "session_req_std": 5, "activity": 1.0},
    "power":  {"population": 0.15, "session_req_mean": 28, "session_req_std": 9, "activity": 2.2},
}

# UA 풀 — 정상 유저 + S7 동적 유저 공통. (단일 IP 안 UA 다양성 → NAT 판정 근거)
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


# ────────────────────────────────────────────────────────────────────────────
# 시간 분포 — 일중 + 요일 패턴
# ────────────────────────────────────────────────────────────────────────────
def diurnal_weight(hour_kst: int) -> float:
    """시각(KST 0~23)에 대한 상대 트래픽 가중치를 0.0 ~ 1.0 으로 반환.

    sample_timestamp() 가 rejection sampling 으로 이 값을 쓴다.
    """
    # 한국 이커머스 일중 곡선: 06시 상승, 13시 점심 피크, 19시 저녁 전역 피크, 04시 바닥.
    HOURLY_WEIGHT = (
        0.30, 0.18, 0.10, 0.06, 0.05, 0.07,   # 00~05  심야 → 바닥(04시)
        0.15, 0.30, 0.45, 0.55, 0.62, 0.70,   # 06~11  상승 (출근·오전)
        0.80, 0.85, 0.82, 0.70, 0.66, 0.70,   # 12~17  점심 피크(13시) 후 소강
        0.82, 1.00, 0.98, 0.90, 0.72, 0.50,   # 18~23  저녁 피크(19시=7pm)
    )
    return HOURLY_WEIGHT[hour_kst % 24]


def dow_weight(weekday: int) -> float:
    """요일(0=월 ~ 6=일)별 상대 트래픽 가중치를 0.0 ~ 1.0 으로 반환.

    sample_timestamp() 가 diurnal_weight(시각) × dow_weight(요일) 로 rejection
    sampling 한다. 일주일(168h) seed 에서 요일 리듬을 baseline 에 새겨,
    request_burst z-score 가 '평소 요일 대비 급증'으로도 의미를 갖게 한다.
    """
    # 한국 이커머스 주간 곡선 — 평일 고르고 일요일 저녁 쇼핑 피크 / 토요일 외출로 소폭↓.
    # (튜닝 포인트 — 시연 분포 보고 조정)
    WEEKLY_WEIGHT = (0.95, 0.92, 0.92, 0.93, 0.97, 0.85, 1.00)  # 월 화 수 목 금 토 일
    return WEEKLY_WEIGHT[weekday % 7]


def sample_timestamp(start: datetime, hours: int) -> datetime:
    """diurnal_weight × dow_weight 기반 rejection sampling 으로 doc 시각 1개를 추출."""
    while True:
        offset = random.uniform(0, hours * 3600)
        ts = start + timedelta(seconds=offset)
        kst = ts.astimezone(KST)
        if random.random() < diurnal_weight(kst.hour) * dow_weight(kst.weekday()):
            return ts


# ────────────────────────────────────────────────────────────────────────────
# 유저 풀
# ────────────────────────────────────────────────────────────────────────────
def build_normal_users():
    """B1+B2+B3 — 200명 정상 유저 풀 생성.

    - sub    : NORMAL_SUB_START 부터 200명 (DB 실유저 활성 코호트, api-server/data.sql)
    - ips    : 유저당 1~3개. HOUSEHOLD_RATE 확률로 2~3명이 primary IP 공유 (가정 NAT)
               → IP-윈도우 unique_subs 가 {1,2,3,4} 실분포 → degeneracy 해소 (B1)
    - asn/org: KR_CARRIERS 5개에서 배정, ip_class 는 전부 cgnat_kr (B2)
    - persona: PERSONAS population 비율로 배정 (B4)
    """
    # persona 배정 풀 — population 비율대로 200개 만들어 셔플 (표본 흔들림 없이 정확)
    persona_pool = []
    for name, p in PERSONAS.items():
        persona_pool += [name] * round(p["population"] * NORMAL_USER_COUNT)
    while len(persona_pool) < NORMAL_USER_COUNT:
        persona_pool.append("normal")
    persona_pool = persona_pool[:NORMAL_USER_COUNT]
    random.shuffle(persona_pool)

    def gen_ip(carrier):
        block = random.choice(carrier["blocks"])
        return f"{block}.{random.randint(0, 255)}.{random.randint(2, 254)}"

    users = []
    i = 0
    while i < NORMAL_USER_COUNT:
        carrier = random.choice(KR_CARRIERS)
        primary_ip = gen_ip(carrier)
        # 가정 NAT — 일부 유저는 primary IP 를 2~3명이 공유
        group = 1
        if random.random() < HOUSEHOLD_RATE:
            group = min(random.randint(2, 3), NORMAL_USER_COUNT - i)
        for _ in range(group):
            sub = str(NORMAL_SUB_START + i)
            # primary(공유 가능) + 개인 모바일 IP 0~2개 (같은 캐리어, 동일 ip_class)
            ips = [primary_ip] + [gen_ip(carrier) for _ in range(random.randint(0, 2))]
            users.append({
                "sub": sub,
                "ips": ips,
                "asn": carrier["asn"],
                "org": carrier["org"],
                "persona": persona_pool[i],
                "lsid": f"lsid-{sub}-{uuid.uuid4().hex[:4]}",
                "ua": random.choice(UA_POOL),
            })
            i += 1
    return users


def make_user(sub, ip):
    """S7 시나리오용 동적 사용자 — 단일 (공유) IP. v3: 정상 유저와 동일 스키마."""
    return {
        "sub": str(sub),
        "ips": [ip],
        "asn": "AS4766",
        "org": "Korea Telecom",
        "persona": "normal",
        "lsid": f"lsid-{sub}-{uuid.uuid4().hex[:4]}",
        "ua": random.choice(UA_POOL),
    }


# ────────────────────────────────────────────────────────────────────────────
# Doc 생성
# ────────────────────────────────────────────────────────────────────────────
def make_jwt_object(user, jti, iat):
    """Filebeat processor 가 분해한 jwt object 형태 그대로.

    jti / iat 는 세션 단위로 정해진다 — 한 세션 = 한 토큰. exp = iat + 600.
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

    # B1 — 유저의 IP 풀(1~3개)에서 이 요청의 client IP 추출.
    client_ip = random.choice(user["ips"])

    return {
        "@timestamp": doc_ts.isoformat().replace("+00:00", "Z"),
        "time": doc_ts.replace(microsecond=0).isoformat(),
        "ip": random.choice(ALB_NGINX_IPS),
        "x_forwarded_for": client_ip,
        "client_ip": client_ip,
        "user_agent": user["ua"],
        "ip_asn": user["asn"],          # B2 — 5개 KR 캐리어 분산
        "ip_org": user["org"],
        "ip_country": "KR",
        "ip_class": "cgnat_kr",         # 한국 통신사 = 전부 cgnat_kr (asn-map.yaml)
        "is_nat_whitelisted": True,
        "ip_class_source": "seed_bulk",   # 정직한 출처 — asn-classify pipeline 우회
        "method": entry["method"],
        "uri": uri,
        "status": str(entry["status"]),
        "bytes_sent": str(bytes_sent),
        "response_time": str(response_time),
        "jwt": make_jwt_object(user, jti, iat),
        "host": {"name": random.choice(PRIV_WEB_HOSTS)},
        # provenance — 매핑 dynamic:false 라 색인 X, _source 보존만.
        "zeti_seed": {
            "source": "scenario_seed",
            "scenario": scenario,
            "baseline_eligible": baseline_eligible,
        },
        "agent": {"type": "filebeat", "version": "8.19.14"},
        "input": {"type": "log"},
        "log": {"file": {"path": "/var/log/nginx/uba.log"}},
        "ecs": {"version": "8.0.0"},
    }


def make_session(user, sess_start, scenario, baseline_eligible):
    """한 세션 = 한 토큰(jti)으로 SESSION_SPAN_SEC 안에 N건 요청을 묶은 클러스터.

    세션 크기 N 은 유저 persona 로 결정된다 (B4) — light 는 짧고 power 는 길다.
    """
    jti = str(uuid.uuid4())
    iat = int(sess_start.timestamp())
    p = PERSONAS[user["persona"]]
    n_req = max(MIN_SESSION_REQ,
                min(MAX_SESSION_REQ,
                    int(random.gauss(p["session_req_mean"], p["session_req_std"]))))
    docs = []
    for _ in range(n_req):
        doc_ts = sess_start + timedelta(seconds=random.uniform(0, SESSION_SPAN_SEC))
        docs.append(make_doc(user, doc_ts, jti, iat, scenario, baseline_eligible))
    return docs


def build_scenario_docs(scenario, hours, normal_count, start):
    """선택된 scenario 의 doc 리스트 생성 (세션 버스트 모델).

    각 시나리오는 목표 doc 수에 도달할 때까지 세션을 반복 생성한다.
    유저 선택은 persona activity 로 가중된다 (B4 — power 가 자주 로그인).
    """
    docs = []

    def _fill(users, target, scen, eligible):
        weights = [PERSONAS[u["persona"]]["activity"] for u in users]
        produced = 0
        while produced < target:
            user = random.choices(users, weights=weights, k=1)[0]
            sess_start = sample_timestamp(start, hours)   # 세션 시작 = 일중·요일 분포
            sess = make_session(user, sess_start, scen, eligible)
            docs.extend(sess)
            produced += len(sess)

    if scenario in ("normal", "all"):
        _fill(build_normal_users(), normal_count, "normal", True)

    if scenario in ("s7-household", "all"):
        # 단일 IP × user 4명 → unique_subs=4 (soft cap fallback 임계 미만 확인용)
        hh_users = [make_user(s, S7_HOUSEHOLD_IP) for s in range(140000700, 140000704)]
        _fill(hh_users, S7_HOUSEHOLD_COUNT, "s7-household", True)

    if scenario in ("s7-cafe", "all"):
        # 단일 IP × user 50명 → unique_subs=50. baseline_eligible=False (오염 방지)
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
    parser.add_argument("--hours", type=int, default=168,
                        help="시간 윈도우 (default 168h=7일 — UBA 표준 baseline 윈도우)")
    parser.add_argument("--batch", type=int, default=500, help="_bulk 배치 크기 (default 500)")
    parser.add_argument("--seed", type=int, default=None, help="random seed (재현 가능 시)")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    normal_count = args.count if args.count is not None else args.hours * DEFAULT_DOCS_PER_HOUR
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=args.hours)

    print(f"=== ZETI Phase 1 baseline seed (v3) ===")
    print(f"  대상 데이터스트림: {TARGET_DATASTREAM}")
    print(f"  시나리오:          {args.scenario}")
    print(f"  시간 윈도우:       {start.isoformat()} ~ {now.isoformat()} ({args.hours}h)")
    print(f"  kid:               {KID}")
    if args.scenario in ("normal", "all"):
        print(f"  normal doc 수:     {normal_count} (user {NORMAL_USER_COUNT}명)")
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
    print(f"    -d '{{\"aggs\":{{\"by_sub\":{{\"terms\":{{\"field\":\"jwt.sub\",\"size\":220}}}},"
          f"\"by_kid\":{{\"terms\":{{\"field\":\"jwt.kid\"}}}},"
          f"\"by_asn\":{{\"terms\":{{\"field\":\"ip_asn\"}}}},"
          f"\"by_ipclass\":{{\"terms\":{{\"field\":\"ip_class\"}}}}}}}}'")


if __name__ == "__main__":
    main()
