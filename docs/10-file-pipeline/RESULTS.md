# 파일 생산기 결과 — STAR

S: Redis 제외 지시와 원문 로그 식별자/중복/누락 문제가 있었다.
T: 피처 계산에 필요한 정보만 가명 HTTP 관측으로 전달한다.
A: 표준 라이브러리 기반 엄격 JSON parser, 목적별 HMAC client/path, request ID 기반 event UUID, capture metadata와 checksum을 구현했다. 원문 IP/UA/path·JWT·Cookie·query는 출력하지 않는다. 불완전 자료를 정상으로 만드는 0 대입을 하지 않는다.
R: 4개 단위 검사 통과(가명화/안정 ID, extra field 거부/bytes null 보존, 중복/손상 capture, ID 충돌 거부). 별도 UBA에서 피처와 파일 탐지 연결을 검증한다. 실제 Nginx 인프라 설정 적용·서버 트래픽 수집은 Claude 담당이며 이번에는 형식 예제를 제공했다.

새 모델 fit, Redis 서비스/클라이언트, 정책/Auth 호출은 구현 범위에 없다. 기존 filebeat/v1 설정과 사용자/Claude checkout은 변경하지 않았다.
