-- 시험 전용 사본. 원본은 공유 계약(zetty-wt/shared/outbox-contract.md, api-server schema.sql)이다.
-- 원본이 바뀌면 이 파일도 같은 내용으로 맞춘다(독립 수정 금지).
CREATE TABLE IF NOT EXISTS security_event_outbox (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id      CHAR(36)     NOT NULL,
  producer      VARCHAR(16)  NOT NULL,
  event_type    VARCHAR(32)  NOT NULL,
  occurred_at   DATETIME(6)  NOT NULL,
  payload       JSON         NOT NULL,
  status        ENUM('PENDING','PUBLISHED') NOT NULL DEFAULT 'PENDING',
  lease_owner   VARCHAR(64)  NULL,
  lease_until   DATETIME(6)  NULL,
  attempts      INT          NOT NULL DEFAULT 0,
  published_at  DATETIME(6)  NULL,
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uk_outbox_event (event_id),
  KEY idx_outbox_pending (status, id)
);
CREATE TABLE IF NOT EXISTS security_event_receipt (
  event_id    CHAR(36)     PRIMARY KEY,
  es_index    VARCHAR(64)  NOT NULL,
  indexed_at  DATETIME(6)  NOT NULL
);
