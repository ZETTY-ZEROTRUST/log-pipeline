-- pipeline 서비스 계정 최소 권한(zeti_db).
-- 계정 생성(CREATE USER ... IDENTIFIED BY ...)과 비밀번호는 이 파일에 두지 않는다. Compose 담당이 secret으로 만든다.
-- host '%'는 예시다. Compose에서는 analysis/data 네트워크 대역으로 좁힌다.
-- 통합 시험(pipeline/tests/integration)이 이 파일을 그대로 적용해 권한이 충분하고 과하지 않음을 확인한다.

-- relay: Outbox 읽기 + 전달 상태 컬럼만 갱신(payload·event_id 등은 바꿀 수 없다).
GRANT SELECT ON zeti_db.security_event_outbox TO 'zetty_relay'@'%';
GRANT UPDATE (status, lease_owner, lease_until, attempts, published_at) ON zeti_db.security_event_outbox TO 'zetty_relay'@'%';

-- indexer: receipt 기록·조회만.
GRANT SELECT, INSERT ON zeti_db.security_event_receipt TO 'zetty_indexer'@'%';

-- 운영 replay(python -m pipeline.recovery): Outbox 상태 되돌리기 + receipt 대조.
GRANT SELECT ON zeti_db.security_event_outbox TO 'zetty_pipeline_ops'@'%';
GRANT UPDATE (status, lease_owner, lease_until) ON zeti_db.security_event_outbox TO 'zetty_pipeline_ops'@'%';
GRANT SELECT ON zeti_db.security_event_receipt TO 'zetty_pipeline_ops'@'%';
