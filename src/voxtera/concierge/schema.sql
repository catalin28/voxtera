-- Voxtera Website Concierge — leads_calls table (MySQL / InnoDB).
--
-- One row per inbound call. The row is created at the Daily pinless dial-in
-- webhook (before the bot answers), then enriched as the conversation
-- progresses. So this single table doubles as the call log AND the lead store:
-- even hang-ups and wrong numbers leave a row.
--
-- status lifecycle: ringing -> answered -> captured -> booked
--                                       \-> abandoned (caller hung up / no data)
--
-- See docs/website-concierge/architecture.md §4 for the data model rationale.

CREATE TABLE IF NOT EXISTS leads_calls (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP,
    caller_number     VARCHAR(32)  NULL,            -- From, via pinless webhook
    dialed_number     VARCHAR(32)  NULL,            -- To (our concierge number)
    status            VARCHAR(24)  NOT NULL DEFAULT 'ringing',
    name              VARCHAR(120) NULL,            -- captured + confirmed
    email             VARCHAR(160) NULL,            -- captured + spell-back confirmed
    phone             VARCHAR(32)  NULL,            -- defaults to caller_number
    timezone          VARCHAR(64)  NULL,            -- needed for the Cal.com booking
    booking_time      DATETIME     NULL,            -- the booked slot, if any
    calcom_booking_id VARCHAR(64)  NULL,            -- returned by Cal.com
    notes             TEXT         NULL,            -- free notes / transcript summary
    PRIMARY KEY (id),
    KEY idx_caller_number (caller_number),
    KEY idx_status (status),
    KEY idx_created_at (created_at)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;
