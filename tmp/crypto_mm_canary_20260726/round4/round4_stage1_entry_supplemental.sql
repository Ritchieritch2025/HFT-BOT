-- KXBTC15M-ROUND4-STAGE1-ENTRY-ROSTER-V1
-- Supplemental DuckDB tables for reservation capital and Stage-1 terminals.
-- The base entry_episode/entry_risk_interval tables remain defined by
-- round4_two_stage_tables.sql.

CREATE TABLE entry_capital_interval (
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    segment_index INTEGER NOT NULL CHECK (segment_index >= 0),
    segment_start_wall_ns BIGINT NOT NULL
        CHECK (segment_start_wall_ns > 0),
    segment_stop_wall_ns BIGINT NOT NULL
        CHECK (segment_stop_wall_ns > segment_start_wall_ns),
    locked_capital_usd DECIMAL(18, 8) NOT NULL
        CHECK (locked_capital_usd >= 0),
    capital_dollar_seconds DECIMAL(28, 8) NOT NULL
        CHECK (capital_dollar_seconds >= 0),
    component_reason VARCHAR NOT NULL
        CHECK (component_reason IN (
            'TWO_RESTING_ENTRY_RESERVATIONS',
            'PENDING_NEW_RESERVATION',
            'ONE_ACKED_ROLLBACK_RESERVATION'
        )),
    PRIMARY KEY (
        entry_episode_id,
        entry_action_id,
        segment_index
    ),
    CHECK (
        capital_dollar_seconds
        = CAST(
            locked_capital_usd
            * CAST(
                segment_stop_wall_ns - segment_start_wall_ns
                AS DECIMAL(28, 8)
            )
            / CAST(1000000000 AS DECIMAL(28, 8))
            AS DECIMAL(28, 8)
        )
    )
);

CREATE TABLE entry_stage1_outcome (
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    source_date_utc DATE NOT NULL,
    market_ticker VARCHAR NOT NULL,
    admitted BOOLEAN NOT NULL,
    terminal_type VARCHAR NOT NULL
        CHECK (terminal_type IN (
            'ENTRY_SKIP',
            'ACK_FAILED',
            'YES_FIRST',
            'NO_FIRST',
            'ADMIN_CENSOR_NO_FIRST_FILL'
        )),
    first_fill_episode_id VARCHAR,
    strict_trade_through_verified BOOLEAN,
    ack_observation_kind VARCHAR NOT NULL
        CHECK (ack_observation_kind IN (
            'NOT_SENT',
            'SHADOW_IMMEDIATE_BOTH_ACKED',
            'OBSERVED_WIRE_ACK',
            'SYNTHETIC_ACK_FAILURE_TEST'
        )),
    entry_quote_seconds DECIMAL(18, 9) NOT NULL
        CHECK (entry_quote_seconds >= 0),
    capital_dollar_seconds DECIMAL(28, 8) NOT NULL
        CHECK (capital_dollar_seconds >= 0),
    peak_episode_capital_usd DECIMAL(18, 8) NOT NULL
        CHECK (peak_episode_capital_usd >= 0),
    reconciliation_ok BOOLEAN NOT NULL,
    PRIMARY KEY (entry_episode_id, entry_action_id),
    CHECK (source_date_utc NOT IN (
        DATE '2026-07-23', DATE '2026-07-26'
    )),
    CHECK (source_date_utc IN (
        DATE '2026-07-20',
        DATE '2026-07-21',
        DATE '2026-07-22'
    )),
    CHECK (
        admitted
        = (
            terminal_type IN (
                'YES_FIRST',
                'NO_FIRST',
                'ADMIN_CENSOR_NO_FIRST_FILL'
            )
        )
    ),
    CHECK (
        (
            terminal_type IN ('YES_FIRST', 'NO_FIRST')
            AND first_fill_episode_id IS NOT NULL
            AND strict_trade_through_verified
        )
        OR (
            terminal_type NOT IN ('YES_FIRST', 'NO_FIRST')
            AND first_fill_episode_id IS NULL
            AND strict_trade_through_verified IS NULL
        )
    ),
    CHECK (reconciliation_ok)
);
