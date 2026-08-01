-- KXBTC15M-ROUND4-TWO-STAGE-HAZARD-V1
-- R1_PRE_DATA_P1: DATA_INVALID rollback + explicit zero-time atom.
-- DuckDB-compatible executable table contract.
-- Money is Decimal dollars; exchange prices are integer e4 dollars.

CREATE TABLE source_day_gate (
    experiment_id VARCHAR NOT NULL,
    source_date_utc DATE NOT NULL,
    data_role VARCHAR NOT NULL
        CHECK (data_role IN ('DISCOVERY', 'FORWARD')),
    path_preopen_allowed BOOLEAN NOT NULL,
    full_utc_day BOOLEAN NOT NULL,
    source_manifest_sha256 VARCHAR NOT NULL,
    l2_receipt_sha256 VARCHAR NOT NULL,
    trades_manifest_sha256 VARCHAR NOT NULL,
    sids_with_seq_gaps BIGINT NOT NULL CHECK (sids_with_seq_gaps >= 0),
    seq_gap_events BIGINT NOT NULL CHECK (seq_gap_events >= 0),
    seq_missed_total BIGINT NOT NULL CHECK (seq_missed_total >= 0),
    seq_regressions BIGINT NOT NULL CHECK (seq_regressions >= 0),
    stream_restarts BIGINT NOT NULL CHECK (stream_restarts >= 0),
    markers_lost_frames BIGINT NOT NULL CHECK (markers_lost_frames >= 0),
    parse_errors BIGINT NOT NULL CHECK (parse_errors >= 0),
    market_days_total BIGINT NOT NULL CHECK (market_days_total >= 0),
    market_days_valid BIGINT NOT NULL CHECK (market_days_valid >= 0),
    gate_pass BOOLEAN NOT NULL,
    gate_reason VARCHAR,
    PRIMARY KEY (experiment_id, source_date_utc),
    CHECK (source_date_utc NOT IN (
        DATE '2026-07-23', DATE '2026-07-26'
    )),
    CHECK (
        data_role <> 'DISCOVERY'
        OR source_date_utc IN (
            DATE '2026-07-20',
            DATE '2026-07-21',
            DATE '2026-07-22'
        )
    ),
    CHECK (
        NOT gate_pass
        OR (
            path_preopen_allowed
            AND full_utc_day
            AND sids_with_seq_gaps = 0
            AND seq_gap_events = 0
            AND seq_missed_total = 0
            AND seq_regressions = 0
            AND stream_restarts = 0
            AND markers_lost_frames = 0
            AND parse_errors = 0
            AND market_days_total = market_days_valid
        )
    )
);

CREATE TABLE action_set_seal (
    experiment_id VARCHAR NOT NULL,
    action_set_version VARCHAR NOT NULL,
    seal_wall_ns BIGINT NOT NULL CHECK (seal_wall_ns > 0),
    first_forward_date_utc DATE NOT NULL,
    status VARCHAR NOT NULL
        CHECK (status IN ('ACTION_SET_PENDING', 'SEALED')),
    entry_actions_sha256 VARCHAR,
    postfill_actions_sha256 VARCHAR,
    model_sha256 VARCHAR,
    scaler_sha256 VARCHAR,
    calibrator_sha256 VARCHAR,
    feature_schema_sha256 VARCHAR,
    table_schema_sha256 VARCHAR NOT NULL,
    replay_code_sha256 VARCHAR NOT NULL,
    source_manifest_sha256 VARCHAR NOT NULL,
    fee_schedule_sha256 VARCHAR NOT NULL,
    risk_ledger_sha256 VARCHAR NOT NULL,
    queue_contract_sha256 VARCHAR NOT NULL,
    latency_contract_sha256 VARCHAR NOT NULL,
    PRIMARY KEY (experiment_id, action_set_version),
    CHECK (first_forward_date_utc NOT IN (
        DATE '2026-07-23', DATE '2026-07-26'
    )),
    CHECK (
        status <> 'SEALED'
        OR (
            entry_actions_sha256 IS NOT NULL
            AND postfill_actions_sha256 IS NOT NULL
            AND model_sha256 IS NOT NULL
            AND scaler_sha256 IS NOT NULL
            AND calibrator_sha256 IS NOT NULL
        )
    )
);

CREATE TABLE entry_episode (
    experiment_id VARCHAR NOT NULL,
    data_role VARCHAR NOT NULL
        CHECK (data_role IN ('DISCOVERY', 'FORWARD')),
    source_date_utc DATE NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_cluster_id VARCHAR NOT NULL,
    cycle_id VARCHAR NOT NULL,
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    action_set_version VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    decision_recv_wall_ns BIGINT NOT NULL CHECK (decision_recv_wall_ns > 0),
    decision_recv_mono_ns BIGINT NOT NULL CHECK (decision_recv_mono_ns > 0),
    feature_asof_wall_ns BIGINT NOT NULL CHECK (feature_asof_wall_ns > 0),
    entry_active_wall_ns BIGINT,
    terminal_wall_ns BIGINT,
    book_ws_sid VARCHAR NOT NULL,
    book_ws_seq BIGINT NOT NULL CHECK (book_ws_seq >= 0),
    action_kind VARCHAR NOT NULL
        CHECK (action_kind IN ('ENTRY_SKIP', 'ENTRY_PAIR_POST_ONLY')),
    yes_price_e4 INTEGER,
    no_price_e4 INTEGER,
    clip_fp DECIMAL(18, 8),
    pair_cost_e4 INTEGER,
    locked_pair_ceiling_e4 INTEGER,
    legal_grid BOOLEAN NOT NULL,
    yes_post_only BOOLEAN NOT NULL,
    no_post_only BOOLEAN NOT NULL,
    ack_state VARCHAR NOT NULL
        CHECK (ack_state IN (
            'NOT_SENT', 'BOTH_ACKED', 'ONE_FAILED', 'UNKNOWN'
        )),
    tte_ms BIGINT NOT NULL CHECK (tte_ms >= 0),
    yes_same_price_ahead_fp DECIMAL(24, 8),
    no_same_price_ahead_fp DECIMAL(24, 8),
    yes_better_depth_fp DECIMAL(24, 8),
    no_better_depth_fp DECIMAL(24, 8),
    yes_queue_position_fp DECIMAL(24, 8),
    no_queue_position_fp DECIMAL(24, 8),
    yes_flow_1s_fp DECIMAL(24, 8),
    no_flow_1s_fp DECIMAL(24, 8),
    yes_flow_5s_fp DECIMAL(24, 8),
    no_flow_5s_fp DECIMAL(24, 8),
    yes_flow_10s_fp DECIMAL(24, 8),
    no_flow_10s_fp DECIMAL(24, 8),
    yes_flow_60s_fp DECIMAL(24, 8),
    no_flow_60s_fp DECIMAL(24, 8),
    touch_imbalance DECIMAL(18, 10),
    spread_e4 INTEGER NOT NULL CHECK (spread_e4 >= 0),
    mid_move_1s_e4 INTEGER,
    mid_move_10s_e4 INTEGER,
    terminal_cause VARCHAR NOT NULL
        CHECK (terminal_cause IN (
            'YES_FIRST',
            'NO_FIRST',
            'ADMIN_CENSOR_NO_FIRST_FILL',
            'ACK_FAILED',
            'ENTRY_SKIP'
        )),
    first_fill_wall_ns BIGINT,
    censor_reason VARCHAR,
    PRIMARY KEY (entry_episode_id, entry_action_id),
    CHECK (source_date_utc NOT IN (
        DATE '2026-07-23', DATE '2026-07-26'
    )),
    CHECK (
        data_role <> 'DISCOVERY'
        OR source_date_utc IN (
            DATE '2026-07-20',
            DATE '2026-07-21',
            DATE '2026-07-22'
        )
    ),
    CHECK (feature_asof_wall_ns <= decision_recv_wall_ns),
    CHECK (
        action_kind <> 'ENTRY_PAIR_POST_ONLY'
        OR (
            yes_price_e4 BETWEEN 1 AND 9999
            AND no_price_e4 BETWEEN 1 AND 9999
            AND clip_fp > 0
            AND pair_cost_e4 = yes_price_e4 + no_price_e4
            AND pair_cost_e4 <= locked_pair_ceiling_e4
        )
    ),
    CHECK (
        first_fill_wall_ns IS NULL
        OR (
            entry_active_wall_ns IS NOT NULL
            AND first_fill_wall_ns >= entry_active_wall_ns
        )
    ),
    CHECK (
        terminal_wall_ns IS NULL
        OR terminal_wall_ns >= decision_recv_wall_ns
    )
);

CREATE TABLE entry_risk_interval (
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    interval_index INTEGER NOT NULL CHECK (interval_index >= 0),
    interval_start_wall_ns BIGINT NOT NULL,
    interval_stop_wall_ns BIGINT NOT NULL,
    feature_asof_wall_ns BIGINT NOT NULL,
    elapsed_start_ms DECIMAL(18, 3) NOT NULL CHECK (elapsed_start_ms >= 0),
    elapsed_stop_ms DECIMAL(18, 3) NOT NULL CHECK (elapsed_stop_ms > 0),
    at_risk BOOLEAN NOT NULL,
    event_yes_first SMALLINT NOT NULL
        CHECK (event_yes_first IN (0, 1)),
    event_no_first SMALLINT NOT NULL
        CHECK (event_no_first IN (0, 1)),
    admin_censor SMALLINT NOT NULL
        CHECK (admin_censor IN (0, 1)),
    data_invalid SMALLINT NOT NULL
        CHECK (data_invalid = 0),
    yes_same_price_ahead_fp DECIMAL(24, 8),
    no_same_price_ahead_fp DECIMAL(24, 8),
    yes_better_depth_fp DECIMAL(24, 8),
    no_better_depth_fp DECIMAL(24, 8),
    yes_flow_10s_fp DECIMAL(24, 8),
    no_flow_10s_fp DECIMAL(24, 8),
    yes_flow_60s_fp DECIMAL(24, 8),
    no_flow_60s_fp DECIMAL(24, 8),
    touch_imbalance DECIMAL(18, 10),
    spread_e4 INTEGER,
    mid_move_1s_e4 INTEGER,
    mid_move_10s_e4 INTEGER,
    tte_ms BIGINT CHECK (tte_ms >= 0),
    yes_order_age_ms BIGINT CHECK (yes_order_age_ms >= 0),
    no_order_age_ms BIGINT CHECK (no_order_age_ms >= 0),
    PRIMARY KEY (
        entry_episode_id, entry_action_id, interval_index
    ),
    CHECK (interval_stop_wall_ns > interval_start_wall_ns),
    CHECK (feature_asof_wall_ns <= interval_start_wall_ns),
    CHECK (elapsed_stop_ms > elapsed_start_ms),
    CHECK (
        event_yes_first
        + event_no_first
        + admin_censor
        + data_invalid
        <= 1
    )
);

CREATE TABLE first_fill_state (
    postfill_episode_id VARCHAR PRIMARY KEY,
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    first_fill_side VARCHAR NOT NULL
        CHECK (first_fill_side IN ('YES', 'NO')),
    first_fill_price_e4 INTEGER NOT NULL
        CHECK (first_fill_price_e4 BETWEEN 1 AND 9999),
    first_fill_qty_fp DECIMAL(18, 8) NOT NULL
        CHECK (first_fill_qty_fp > 0),
    first_fill_fee_usd DECIMAL(18, 8) NOT NULL
        CHECK (first_fill_fee_usd >= 0),
    first_fill_recv_wall_ns BIGINT NOT NULL,
    first_fill_recv_mono_ns BIGINT NOT NULL,
    feature_asof_wall_ns BIGINT NOT NULL,
    first_fill_elapsed_ms DECIMAL(18, 3) NOT NULL
        CHECK (first_fill_elapsed_ms >= 0),
    complement_order_id VARCHAR NOT NULL,
    complement_side VARCHAR NOT NULL
        CHECK (complement_side IN ('YES', 'NO')),
    complement_price_e4 INTEGER NOT NULL
        CHECK (complement_price_e4 BETWEEN 1 AND 9999),
    complement_remaining_qty_fp DECIMAL(18, 8) NOT NULL
        CHECK (complement_remaining_qty_fp >= 0),
    complement_order_age_ms BIGINT NOT NULL
        CHECK (complement_order_age_ms >= 0),
    complement_queue_position_fp DECIMAL(24, 8),
    complement_same_price_ahead_fp DECIMAL(24, 8),
    complement_better_depth_fp DECIMAL(24, 8),
    complement_flow_1s_fp DECIMAL(24, 8),
    complement_flow_5s_fp DECIMAL(24, 8),
    complement_flow_10s_fp DECIMAL(24, 8),
    complement_flow_60s_fp DECIMAL(24, 8),
    spread_e4 INTEGER NOT NULL CHECK (spread_e4 >= 0),
    mid_move_since_entry_e4 INTEGER,
    mid_move_since_fill_e4 INTEGER,
    pair_gain_if_complement_usd DECIMAL(18, 8) NOT NULL,
    buy_complement_exit_pnl_usd DECIMAL(18, 8) NOT NULL,
    sell_first_exit_pnl_usd DECIMAL(18, 8) NOT NULL,
    buy_complement_executable_qty_fp DECIMAL(18, 8) NOT NULL,
    sell_first_executable_qty_fp DECIMAL(18, 8) NOT NULL,
    tte_ms BIGINT NOT NULL CHECK (tte_ms >= 0),
    cancel_state VARCHAR NOT NULL
        CHECK (cancel_state IN (
            'NONE', 'PENDING', 'ACKED', 'UNKNOWN'
        )),
    CHECK (feature_asof_wall_ns <= first_fill_recv_wall_ns),
    CHECK (first_fill_side <> complement_side),
    UNIQUE (entry_episode_id, entry_action_id)
);

-- A same-envelope complement fill is a discrete mass at elapsed zero.
-- It has no duration column and must never be converted to an epsilon risk
-- interval or inserted into postfill_risk_interval.
CREATE TABLE postfill_zero_time_atom (
    postfill_episode_id VARCHAR PRIMARY KEY,
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    market_ticker VARCHAR
        CHECK (market_ticker LIKE 'KXBTC15M-%'),
    -- Nullable only for backward compatibility with the sealed Stage-1
    -- builder.  V4 Stage-2 always supplies this complete provenance bundle.
    data_origin VARCHAR CHECK (data_origin = 'PUBLIC_RAW'),
    first_fill_provenance VARCHAR CHECK (
        first_fill_provenance
        = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
    ),
    first_fill_execution_nature VARCHAR
        CHECK (first_fill_execution_nature = 'SYNTHETIC'),
    first_fill_fee_provenance VARCHAR
        CHECK (first_fill_fee_provenance = 'SCHEDULE_ZERO_MAKER'),
    first_fill_side VARCHAR NOT NULL
        CHECK (first_fill_side IN ('YES', 'NO')),
    complement_side VARCHAR NOT NULL
        CHECK (complement_side IN ('YES', 'NO')),
    atom_recv_wall_ns BIGINT NOT NULL CHECK (atom_recv_wall_ns > 0),
    atom_recv_mono_ns BIGINT NOT NULL CHECK (atom_recv_mono_ns > 0),
    receipt_envelope_id VARCHAR NOT NULL,
    first_fill_stable_source_id VARCHAR NOT NULL,
    complement_fill_stable_source_id VARCHAR NOT NULL,
    complement_fill_price_e4 INTEGER NOT NULL
        CHECK (complement_fill_price_e4 BETWEEN 1 AND 9999),
    complement_fill_qty_fp DECIMAL(18, 8) NOT NULL
        CHECK (complement_fill_qty_fp > 0),
    complement_fill_fee_usd DECIMAL(18, 8) NOT NULL
        CHECK (complement_fill_fee_usd = 0),
    complement_fill_provenance VARCHAR CHECK (
        complement_fill_provenance
        = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
    ),
    complement_execution_nature VARCHAR
        CHECK (complement_execution_nature = 'SYNTHETIC'),
    complement_fee_provenance VARCHAR
        CHECK (complement_fee_provenance = 'SCHEDULE_ZERO_MAKER'),
    reconciliation_ok BOOLEAN NOT NULL,
    UNIQUE (entry_episode_id, entry_action_id),
    CHECK (first_fill_side <> complement_side),
    CHECK (
        first_fill_stable_source_id
        < complement_fill_stable_source_id
    ),
    CHECK (
        (
            data_origin IS NULL
            AND market_ticker IS NULL
            AND first_fill_provenance IS NULL
            AND first_fill_execution_nature IS NULL
            AND first_fill_fee_provenance IS NULL
            AND complement_fill_provenance IS NULL
            AND complement_execution_nature IS NULL
            AND complement_fee_provenance IS NULL
        )
        OR
        (
            market_ticker LIKE 'KXBTC15M-%'
            AND data_origin = 'PUBLIC_RAW'
            AND first_fill_provenance
                = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
            AND first_fill_execution_nature = 'SYNTHETIC'
            AND first_fill_fee_provenance = 'SCHEDULE_ZERO_MAKER'
            AND complement_fill_provenance
                = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
            AND complement_execution_nature = 'SYNTHETIC'
            AND complement_fee_provenance = 'SCHEDULE_ZERO_MAKER'
        )
    ),
    CHECK (reconciliation_ok)
);

CREATE TABLE postfill_decision (
    postfill_decision_id VARCHAR PRIMARY KEY,
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL CHECK (decision_index >= 0),
    decision_recv_wall_ns BIGINT NOT NULL,
    decision_recv_mono_ns BIGINT NOT NULL,
    feature_asof_wall_ns BIGINT NOT NULL,
    elapsed_since_first_fill_ms DECIMAL(18, 3) NOT NULL
        CHECK (elapsed_since_first_fill_ms >= 0),
    first_side VARCHAR NOT NULL CHECK (first_side IN ('YES', 'NO')),
    first_price_e4 INTEGER NOT NULL
        CHECK (first_price_e4 BETWEEN 1 AND 9999),
    remaining_inventory_fp DECIMAL(18, 8) NOT NULL
        CHECK (remaining_inventory_fp > 0),
    complement_price_e4 INTEGER,
    complement_order_age_ms BIGINT CHECK (complement_order_age_ms >= 0),
    complement_queue_position_fp DECIMAL(24, 8),
    complement_same_price_ahead_fp DECIMAL(24, 8),
    complement_better_depth_fp DECIMAL(24, 8),
    complement_flow_1s_fp DECIMAL(24, 8),
    complement_flow_5s_fp DECIMAL(24, 8),
    complement_flow_10s_fp DECIMAL(24, 8),
    complement_flow_60s_fp DECIMAL(24, 8),
    spread_e4 INTEGER CHECK (spread_e4 >= 0),
    mid_move_since_entry_e4 INTEGER,
    mid_move_since_fill_e4 INTEGER,
    buy_complement_exit_pnl_usd DECIMAL(18, 8) NOT NULL,
    sell_first_exit_pnl_usd DECIMAL(18, 8) NOT NULL,
    pair_gain_if_complement_usd DECIMAL(18, 8),
    tte_ms BIGINT NOT NULL CHECK (tte_ms >= 0),
    cancel_state VARCHAR NOT NULL
        CHECK (cancel_state IN (
            'NONE', 'PENDING', 'ACKED', 'UNKNOWN'
        )),
    UNIQUE (postfill_episode_id, decision_index),
    CHECK (feature_asof_wall_ns <= decision_recv_wall_ns)
);

CREATE TABLE postfill_action (
    postfill_action_id VARCHAR PRIMARY KEY,
    postfill_decision_id VARCHAR NOT NULL,
    action_set_version VARCHAR NOT NULL,
    action_kind VARCHAR NOT NULL
        CHECK (action_kind IN ('KEEP', 'REPRICE', 'IOC')),
    complement_old_price_e4 INTEGER
        CHECK (complement_old_price_e4 BETWEEN 1 AND 9999),
    complement_new_price_e4 INTEGER
        CHECK (complement_new_price_e4 BETWEEN 1 AND 9999),
    reprice_tick_offset INTEGER,
    ioc_route VARCHAR
        CHECK (ioc_route IS NULL OR ioc_route IN (
            'BUY_COMPLEMENT', 'SELL_FIRST_LEG'
        )),
    ioc_limit_price_e4 INTEGER
        CHECK (
            ioc_limit_price_e4 IS NULL
            OR ioc_limit_price_e4 BETWEEN 1 AND 9999
        ),
    requested_qty_fp DECIMAL(18, 8) NOT NULL
        CHECK (requested_qty_fp > 0),
    pair_cost_ceiling_e4 INTEGER,
    post_only BOOLEAN NOT NULL,
    cancel_ack_latency_ms DECIMAL(18, 3) NOT NULL
        CHECK (cancel_ack_latency_ms >= 0),
    legal_action BOOLEAN NOT NULL,
    skip_reason VARCHAR,
    CHECK (
        action_kind <> 'KEEP'
        OR (
            complement_new_price_e4 = complement_old_price_e4
            AND ioc_route IS NULL
            AND post_only
        )
    ),
    CHECK (
        action_kind <> 'REPRICE'
        OR (
            complement_new_price_e4 IS NOT NULL
            AND reprice_tick_offset IS NOT NULL
            AND ioc_route IS NULL
            AND post_only
        )
    ),
    CHECK (
        action_kind <> 'IOC'
        OR (
            ioc_route IS NOT NULL
            AND ioc_limit_price_e4 IS NOT NULL
            AND NOT post_only
        )
    )
);

CREATE TABLE postfill_risk_interval (
    postfill_action_id VARCHAR NOT NULL,
    interval_index INTEGER NOT NULL CHECK (interval_index >= 0),
    interval_start_wall_ns BIGINT NOT NULL,
    interval_stop_wall_ns BIGINT NOT NULL,
    feature_asof_wall_ns BIGINT NOT NULL,
    elapsed_start_ms DECIMAL(18, 3) NOT NULL CHECK (elapsed_start_ms >= 0),
    elapsed_stop_ms DECIMAL(18, 3) NOT NULL CHECK (elapsed_stop_ms > 0),
    at_risk BOOLEAN NOT NULL,
    event_complement_fill SMALLINT NOT NULL
        CHECK (event_complement_fill IN (0, 1)),
    event_inventory_exit SMALLINT NOT NULL
        CHECK (event_inventory_exit IN (0, 1)),
    admin_censor SMALLINT NOT NULL
        CHECK (admin_censor IN (0, 1)),
    data_invalid SMALLINT NOT NULL
        CHECK (data_invalid = 0),
    complement_price_e4 INTEGER
        CHECK (complement_price_e4 BETWEEN 1 AND 9999),
    complement_order_age_ms BIGINT CHECK (complement_order_age_ms >= 0),
    complement_queue_position_fp DECIMAL(24, 8),
    complement_same_price_ahead_fp DECIMAL(24, 8),
    complement_better_depth_fp DECIMAL(24, 8),
    complement_flow_1s_fp DECIMAL(24, 8),
    complement_flow_5s_fp DECIMAL(24, 8),
    complement_flow_10s_fp DECIMAL(24, 8),
    complement_flow_60s_fp DECIMAL(24, 8),
    spread_e4 INTEGER CHECK (spread_e4 >= 0),
    mid_move_since_fill_e4 INTEGER,
    buy_complement_exit_pnl_usd DECIMAL(18, 8),
    sell_first_exit_pnl_usd DECIMAL(18, 8),
    pair_gain_if_complement_usd DECIMAL(18, 8),
    tte_ms BIGINT CHECK (tte_ms >= 0),
    cancel_state VARCHAR
        CHECK (cancel_state IN (
            'NONE', 'PENDING', 'ACKED', 'UNKNOWN'
        )),
    PRIMARY KEY (postfill_action_id, interval_index),
    CHECK (interval_stop_wall_ns > interval_start_wall_ns),
    CHECK (feature_asof_wall_ns <= interval_start_wall_ns),
    CHECK (elapsed_stop_ms > elapsed_start_ms),
    CHECK (
        event_complement_fill
        + event_inventory_exit
        + admin_censor
        + data_invalid
        <= 1
    )
);

CREATE TABLE postfill_action_outcome (
    postfill_action_id VARCHAR PRIMARY KEY,
    terminal_type VARCHAR NOT NULL
        CHECK (terminal_type IN (
            'COMPLEMENT_FILL',
            'INVENTORY_EXIT',
            'ADMIN_CENSOR',
            'IOC_PARTIAL_RESIDUAL'
        )),
    terminal_wall_ns BIGINT NOT NULL,
    complement_fill_qty_fp DECIMAL(18, 8) NOT NULL
        CHECK (complement_fill_qty_fp >= 0),
    inventory_exit_qty_fp DECIMAL(18, 8) NOT NULL
        CHECK (inventory_exit_qty_fp >= 0),
    residual_inventory_fp DECIMAL(18, 8) NOT NULL
        CHECK (residual_inventory_fp >= 0),
    maker_fee_usd DECIMAL(18, 8) NOT NULL CHECK (maker_fee_usd >= 0),
    taker_fee_usd DECIMAL(18, 8) NOT NULL CHECK (taker_fee_usd >= 0),
    gross_pnl_usd DECIMAL(18, 8) NOT NULL,
    net_pnl_usd DECIMAL(18, 8) NOT NULL,
    capital_dollar_seconds DECIMAL(28, 8) NOT NULL
        CHECK (capital_dollar_seconds >= 0),
    source_rows_sha256 VARCHAR NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    CHECK (
        net_pnl_usd = gross_pnl_usd - maker_fee_usd - taker_fee_usd
    ),
    CHECK (
        terminal_type <> 'COMPLEMENT_FILL'
        OR residual_inventory_fp = 0
    )
);

CREATE TABLE cycle_policy_outcome (
    experiment_id VARCHAR NOT NULL,
    data_role VARCHAR NOT NULL
        CHECK (data_role IN ('DISCOVERY', 'FORWARD')),
    source_date_utc DATE NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_cluster_id VARCHAR NOT NULL,
    cycle_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    policy_id VARCHAR NOT NULL,
    action_set_version VARCHAR NOT NULL,
    admitted BOOLEAN NOT NULL,
    terminal_type VARCHAR NOT NULL
        CHECK (terminal_type IN (
            'NO_ENTRY',
            'NO_FIRST_FILL',
            'PAIR_COMPLETE',
            'INVENTORY_EXIT',
            'DATA_INVALID',
            'UNRECONCILED'
        )),
    first_fill_side VARCHAR
        CHECK (first_fill_side IS NULL OR first_fill_side IN ('YES', 'NO')),
    net_pnl_usd DECIMAL(18, 8) NOT NULL,
    capital_dollar_seconds DECIMAL(28, 8) NOT NULL
        CHECK (capital_dollar_seconds >= 0),
    peak_episode_capital_usd DECIMAL(18, 8) NOT NULL
        CHECK (peak_episode_capital_usd >= 0),
    entry_quote_seconds DECIMAL(18, 6) NOT NULL
        CHECK (entry_quote_seconds >= 0),
    orphan_seconds DECIMAL(18, 6) NOT NULL
        CHECK (orphan_seconds >= 0),
    maker_fee_usd DECIMAL(18, 8) NOT NULL CHECK (maker_fee_usd >= 0),
    taker_fee_usd DECIMAL(18, 8) NOT NULL CHECK (taker_fee_usd >= 0),
    source_rows_sha256 VARCHAR NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    PRIMARY KEY (
        experiment_id,
        cycle_id,
        entry_action_id,
        policy_id,
        action_set_version
    ),
    CHECK (source_date_utc NOT IN (
        DATE '2026-07-23', DATE '2026-07-26'
    )),
    CHECK (
        data_role <> 'DISCOVERY'
        OR source_date_utc IN (
            DATE '2026-07-20',
            DATE '2026-07-21',
            DATE '2026-07-22'
        )
    ),
    CHECK (
        NOT admitted
        OR terminal_type NOT IN ('NO_ENTRY', 'DATA_INVALID', 'UNRECONCILED')
    )
);

-- Required analysis views.  These include no-fill admitted cycles.
CREATE VIEW policy_ev_cycle AS
SELECT
    experiment_id,
    data_role,
    action_set_version,
    policy_id,
    COUNT(*) FILTER (WHERE admitted) AS admitted_cycles,
    COUNT(DISTINCT market_cluster_id) FILTER (WHERE admitted)
        AS market_clusters,
    SUM(net_pnl_usd) FILTER (WHERE admitted) AS total_net_pnl_usd,
    AVG(net_pnl_usd) FILTER (WHERE admitted) AS ev_usd_per_cycle
FROM cycle_policy_outcome
GROUP BY
    experiment_id, data_role, action_set_version, policy_id;

CREATE VIEW policy_ev_capital_time AS
SELECT
    experiment_id,
    data_role,
    action_set_version,
    policy_id,
    SUM(net_pnl_usd) FILTER (WHERE admitted)
        / NULLIF(
            SUM(capital_dollar_seconds) FILTER (WHERE admitted),
            DECIMAL '0'
        ) AS ev_usd_per_locked_dollar_second,
    DECIMAL '360000'
        * SUM(net_pnl_usd) FILTER (WHERE admitted)
        / NULLIF(
            SUM(capital_dollar_seconds) FILTER (WHERE admitted),
            DECIMAL '0'
        ) AS cents_per_locked_dollar_hour,
    SUM(capital_dollar_seconds) FILTER (WHERE admitted)
        AS total_capital_dollar_seconds
FROM cycle_policy_outcome
GROUP BY
    experiment_id, data_role, action_set_version, policy_id;
