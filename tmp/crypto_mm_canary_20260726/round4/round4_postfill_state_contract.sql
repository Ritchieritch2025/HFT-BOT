-- ROUND4_POSTFILL_PUBLIC_PROXY_V4
-- Discovery-only supplemental schema.  Historical executions are synthetic
-- public-raw counterfactuals/proxies; this schema makes that provenance
-- explicit and must not be read as an account execution receipt.

CREATE TABLE postfill_compact_causal_state (
    postfill_decision_id VARCHAR PRIMARY KEY,
    experiment_id VARCHAR NOT NULL,
    data_role VARCHAR NOT NULL CHECK (data_role = 'DISCOVERY'),
    data_origin VARCHAR NOT NULL CHECK (data_origin = 'PUBLIC_RAW'),
    first_fill_provenance VARCHAR NOT NULL CHECK (
        first_fill_provenance
        = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
    ),
    first_fill_execution_nature VARCHAR NOT NULL
        CHECK (first_fill_execution_nature = 'SYNTHETIC'),
    first_fill_fee_provenance VARCHAR NOT NULL
        CHECK (first_fill_fee_provenance = 'SCHEDULE_ZERO_MAKER'),
    public_book_data_origin VARCHAR NOT NULL
        CHECK (public_book_data_origin = 'PUBLIC_RAW'),
    flatten_counterfactual_provenance VARCHAR NOT NULL CHECK (
        flatten_counterfactual_provenance
        = 'PUBLIC_BOOK_COUNTERFACTUAL'
    ),
    source_date_utc DATE NOT NULL,
    market_ticker VARCHAR NOT NULL
        CHECK (market_ticker LIKE 'KXBTC15M-%'),
    postfill_episode_id VARCHAR NOT NULL,
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL CHECK (decision_index BETWEEN 0 AND 8),
    decision_recv_wall_ns BIGINT NOT NULL CHECK (decision_recv_wall_ns > 0),
    decision_recv_mono_ns BIGINT NOT NULL CHECK (decision_recv_mono_ns > 0),
    feature_asof_wall_ns BIGINT NOT NULL CHECK (feature_asof_wall_ns > 0),
    source_max_recv_wall_ns BIGINT NOT NULL
        CHECK (source_max_recv_wall_ns > 0),
    source_max_recv_mono_ns BIGINT NOT NULL
        CHECK (source_max_recv_mono_ns > 0),
    source_max_stable_id VARCHAR NOT NULL,
    episode_source_rows_sha256 VARCHAR NOT NULL
        CHECK (length(episode_source_rows_sha256) = 64),
    causal_source_rows_sha256 VARCHAR NOT NULL
        CHECK (length(causal_source_rows_sha256) = 64),
    paired_state_fingerprint_sha256 VARCHAR NOT NULL
        CHECK (length(paired_state_fingerprint_sha256) = 64),
    elapsed_since_first_fill_ms BIGINT NOT NULL
        CHECK (elapsed_since_first_fill_ms IN (
            0, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000
        )),
    first_fill_recv_wall_ns BIGINT NOT NULL
        CHECK (first_fill_recv_wall_ns > 0),
    first_fill_recv_mono_ns BIGINT NOT NULL
        CHECK (first_fill_recv_mono_ns > 0),
    first_fill_elapsed_ms DECIMAL(24, 6) NOT NULL
        CHECK (first_fill_elapsed_ms >= 0),
    first_side VARCHAR NOT NULL CHECK (first_side IN ('YES', 'NO')),
    first_price_e4 INTEGER NOT NULL
        CHECK (first_price_e4 BETWEEN 1 AND 9999),
    first_qty_fp DECIMAL(28, 12) NOT NULL CHECK (first_qty_fp > 0),
    first_fill_fee_usd DECIMAL(28, 12) NOT NULL
        CHECK (first_fill_fee_usd = 0),
    first_leg_cost_basis_usd DECIMAL(28, 12) NOT NULL
        CHECK (first_leg_cost_basis_usd > 0),
    remaining_inventory_fp DECIMAL(28, 12) NOT NULL
        CHECK (
            remaining_inventory_fp > 0
            AND remaining_inventory_fp = first_qty_fp
        ),
    complement_order_id VARCHAR NOT NULL,
    complement_side VARCHAR NOT NULL
        CHECK (complement_side IN ('YES', 'NO')),
    complement_price_e4 INTEGER NOT NULL
        CHECK (complement_price_e4 BETWEEN 1 AND 9999),
    complement_order_age_ms BIGINT NOT NULL
        CHECK (complement_order_age_ms >= 0),
    complement_queue_position_fp DECIMAL(28, 12) NOT NULL
        CHECK (complement_queue_position_fp >= 0),
    complement_same_price_ahead_fp DECIMAL(28, 12) NOT NULL
        CHECK (complement_same_price_ahead_fp >= 0),
    complement_better_depth_fp DECIMAL(28, 12) NOT NULL
        CHECK (complement_better_depth_fp >= 0),
    complement_touch_distance_e4 INTEGER
        CHECK (complement_touch_distance_e4 >= 0),
    complement_flow_1s_fp DECIMAL(28, 12) NOT NULL,
    complement_flow_5s_fp DECIMAL(28, 12) NOT NULL,
    complement_flow_10s_fp DECIMAL(28, 12) NOT NULL,
    complement_flow_60s_fp DECIMAL(28, 12) NOT NULL,
    complement_flow_acceleration_fp DECIMAL(28, 12) NOT NULL,
    touch_imbalance DECIMAL(18, 12)
        CHECK (touch_imbalance BETWEEN -1 AND 1),
    spread_e4 INTEGER CHECK (spread_e4 >= 0),
    mid_move_1s_e4 INTEGER,
    mid_move_10s_e4 INTEGER,
    mid_move_since_entry_e4 INTEGER,
    mid_move_since_fill_e4 INTEGER,
    kernel_fair_available BOOLEAN NOT NULL,
    kernel_fair_e4 INTEGER
        CHECK (kernel_fair_e4 BETWEEN 0 AND 10000),
    first_leg_fair_edge_e4 INTEGER,
    complement_fair_edge_e4 INTEGER,
    kernel_fair_move_since_entry_e4 INTEGER,
    kernel_fair_move_since_fill_e4 INTEGER,
    kernel_source_time_ms BIGINT,
    kernel_causality_kind VARCHAR,
    flatten_book_side VARCHAR NOT NULL
        CHECK (flatten_book_side IN ('BID', 'ASK')),
    flatten_limit_price_e4 INTEGER NOT NULL
        CHECK (flatten_limit_price_e4 BETWEEN 10 AND 9990),
    flatten_limit_fallback BOOLEAN NOT NULL,
    flatten_adverse_tick_e4 INTEGER NOT NULL
        CHECK (flatten_adverse_tick_e4 >= 0),
    flatten_visible_slices_sha256 VARCHAR NOT NULL
        CHECK (length(flatten_visible_slices_sha256) = 64),
    flatten_visible_gross_pnl_usd DECIMAL(28, 12) NOT NULL,
    flatten_visible_pnl_fee_low_usd DECIMAL(28, 12) NOT NULL,
    flatten_visible_pnl_fee_base_usd DECIMAL(28, 12) NOT NULL,
    flatten_visible_pnl_fee_high_usd DECIMAL(28, 12) NOT NULL,
    flatten_visible_executable_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (flatten_visible_executable_qty_fp >= 0),
    flatten_visible_residual_inventory_fp DECIMAL(28, 12) NOT NULL
        CHECK (flatten_visible_residual_inventory_fp >= 0),
    two_sided_touch_available BOOLEAN NOT NULL,
    complement_touch_available BOOLEAN NOT NULL,
    simulated_strategy_order_registry_complete BOOLEAN NOT NULL,
    simulated_strategy_order_registry_sha256 VARCHAR NOT NULL
        CHECK (length(simulated_strategy_order_registry_sha256) = 64),
    pair_gain_if_complement_usd DECIMAL(28, 12) NOT NULL,
    tte_ms BIGINT NOT NULL CHECK (tte_ms >= 0),
    cancel_state VARCHAR NOT NULL
        CHECK (cancel_state IN ('NONE', 'PENDING', 'ACKED', 'UNKNOWN')),
    market_day_gate_pass BOOLEAN NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    data_invalid BOOLEAN NOT NULL,
    UNIQUE (postfill_episode_id, decision_index),
    CHECK (source_date_utc IN (
        DATE '2026-07-20',
        DATE '2026-07-21',
        DATE '2026-07-22'
    )),
    CHECK (source_date_utc NOT IN (
        DATE '2026-07-23', DATE '2026-07-26'
    )),
    CHECK (
        (decision_index = 0 AND elapsed_since_first_fill_ms = 0)
        OR (decision_index = 1 AND elapsed_since_first_fill_ms = 250)
        OR (decision_index = 2 AND elapsed_since_first_fill_ms = 500)
        OR (decision_index = 3 AND elapsed_since_first_fill_ms = 1000)
        OR (decision_index = 4 AND elapsed_since_first_fill_ms = 2000)
        OR (decision_index = 5 AND elapsed_since_first_fill_ms = 5000)
        OR (decision_index = 6 AND elapsed_since_first_fill_ms = 10000)
        OR (decision_index = 7 AND elapsed_since_first_fill_ms = 30000)
        OR (decision_index = 8 AND elapsed_since_first_fill_ms = 60000)
    ),
    CHECK (
        decision_recv_wall_ns
        = first_fill_recv_wall_ns
          + elapsed_since_first_fill_ms * 1000000
    ),
    CHECK (
        decision_recv_mono_ns
        = first_fill_recv_mono_ns
          + elapsed_since_first_fill_ms * 1000000
    ),
    CHECK (
        first_fill_recv_wall_ns <= source_max_recv_wall_ns
        AND source_max_recv_wall_ns <= feature_asof_wall_ns
        AND feature_asof_wall_ns <= decision_recv_wall_ns
    ),
    CHECK (
        first_fill_recv_mono_ns <= source_max_recv_mono_ns
        AND source_max_recv_mono_ns <= decision_recv_mono_ns
    ),
    CHECK (first_side <> complement_side),
    CHECK (
        first_leg_cost_basis_usd
        = CAST(
            first_price_e4 * first_qty_fp / 10000
            AS DECIMAL(28, 12)
          )
    ),
    CHECK (
        (first_side = 'YES' AND flatten_book_side = 'ASK')
        OR (first_side = 'NO' AND flatten_book_side = 'BID')
    ),
    CHECK (
        (
            (flatten_limit_price_e4 < 1000
             OR flatten_limit_price_e4 > 9000)
            AND flatten_limit_price_e4 % 10 = 0
        )
        OR (
            flatten_limit_price_e4 BETWEEN 1000 AND 9000
            AND flatten_limit_price_e4 % 100 = 0
        )
    ),
    CHECK (
        flatten_visible_pnl_fee_low_usd
        >= flatten_visible_pnl_fee_base_usd
        AND flatten_visible_pnl_fee_base_usd
        >= flatten_visible_pnl_fee_high_usd
    ),
    CHECK (
        flatten_visible_executable_qty_fp
        + flatten_visible_residual_inventory_fp
        = remaining_inventory_fp
    ),
    CHECK (
        pair_gain_if_complement_usd
        = CAST(
            (
                10000 - first_price_e4 - complement_price_e4
              ) * remaining_inventory_fp / 10000
            AS DECIMAL(28, 12)
          )
    ),
    CHECK (
        (
            two_sided_touch_available
            AND complement_touch_available
            AND complement_touch_distance_e4 IS NOT NULL
            AND touch_imbalance IS NOT NULL
            AND spread_e4 IS NOT NULL
            AND mid_move_1s_e4 IS NOT NULL
            AND mid_move_10s_e4 IS NOT NULL
            AND mid_move_since_entry_e4 IS NOT NULL
            AND mid_move_since_fill_e4 IS NOT NULL
        )
        OR (
            NOT two_sided_touch_available
            AND touch_imbalance IS NULL
            AND spread_e4 IS NULL
            AND mid_move_1s_e4 IS NULL
            AND mid_move_10s_e4 IS NULL
            AND mid_move_since_entry_e4 IS NULL
            AND mid_move_since_fill_e4 IS NULL
        )
    ),
    CHECK (
        (complement_touch_available
         AND complement_touch_distance_e4 IS NOT NULL)
        OR
        (NOT complement_touch_available
         AND complement_touch_distance_e4 IS NULL)
    ),
    CHECK (
        NOT kernel_fair_available
        AND kernel_fair_e4 IS NULL
        AND first_leg_fair_edge_e4 IS NULL
        AND complement_fair_edge_e4 IS NULL
        AND kernel_fair_move_since_entry_e4 IS NULL
        AND kernel_fair_move_since_fill_e4 IS NULL
        AND kernel_source_time_ms IS NULL
        AND kernel_causality_kind IS NULL
    ),
    CHECK (simulated_strategy_order_registry_complete),
    CHECK (market_day_gate_pass),
    CHECK (reconciliation_ok),
    CHECK (NOT data_invalid)
);

CREATE TABLE postfill_compact_causal_action (
    postfill_action_id VARCHAR PRIMARY KEY,
    postfill_decision_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    source_date_utc DATE NOT NULL,
    decision_index INTEGER NOT NULL CHECK (decision_index BETWEEN 0 AND 8),
    action_family_version VARCHAR NOT NULL
        CHECK (
            action_family_version
            = 'ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4'
        ),
    action_kind VARCHAR NOT NULL
        CHECK (action_kind IN ('KEEP', 'FLATTEN_FOK')),
    action_execution_provenance VARCHAR NOT NULL CHECK (
        action_execution_provenance IN (
            'ORIGINAL_MAKER_ORDER_PUBLIC_PROXY_OR_SETTLEMENT',
            'PUBLIC_BOOK_COUNTERFACTUAL'
        )
    ),
    cancel_timing_provenance VARCHAR CHECK (
        cancel_timing_provenance = 'SYNTHETIC_60MS_SCENARIO'
    ),
    self_cross_scope VARCHAR CHECK (
        self_cross_scope = 'SIMULATED_STRATEGY_ORDER_REGISTRY'
    ),
    causal_source_rows_sha256 VARCHAR NOT NULL
        CHECK (length(causal_source_rows_sha256) = 64),
    paired_state_fingerprint_sha256 VARCHAR NOT NULL
        CHECK (length(paired_state_fingerprint_sha256) = 64),
    complement_old_price_e4 INTEGER NOT NULL
        CHECK (complement_old_price_e4 BETWEEN 1 AND 9999),
    complement_new_price_e4 INTEGER
        CHECK (
            complement_new_price_e4 IS NULL
            OR complement_new_price_e4 BETWEEN 1 AND 9999
        ),
    fok_book_side VARCHAR CHECK (fok_book_side IN ('BID', 'ASK')),
    fok_limit_price_e4 INTEGER
        CHECK (
            fok_limit_price_e4 IS NULL
            OR fok_limit_price_e4 BETWEEN 10 AND 9990
        ),
    fok_limit_fallback BOOLEAN,
    requested_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (requested_qty_fp > 0),
    post_only BOOLEAN NOT NULL,
    reduce_only BOOLEAN NOT NULL,
    effective_latency_ms DECIMAL(24, 6) NOT NULL
        CHECK (effective_latency_ms >= 0),
    time_in_force VARCHAR CHECK (time_in_force = 'fill_or_kill'),
    self_trade_prevention_type VARCHAR
        CHECK (self_trade_prevention_type = 'taker_at_cross'),
    fok_requires_prior_synthetic_cancel_applied BOOLEAN NOT NULL,
    profit_gate_bypassed_for_risk_exit BOOLEAN NOT NULL,
    legal_action BOOLEAN NOT NULL,
    skip_reason VARCHAR,
    UNIQUE (
        postfill_decision_id,
        action_family_version,
        action_kind
    ),
    CHECK (source_date_utc IN (
        DATE '2026-07-20',
        DATE '2026-07-21',
        DATE '2026-07-22'
    )),
    CHECK (
        action_kind <> 'KEEP'
        OR (
            complement_new_price_e4 = complement_old_price_e4
            AND fok_book_side IS NULL
            AND fok_limit_price_e4 IS NULL
            AND fok_limit_fallback IS NULL
            AND post_only
            AND NOT reduce_only
            AND effective_latency_ms = 0
            AND time_in_force IS NULL
            AND self_trade_prevention_type IS NULL
            AND NOT fok_requires_prior_synthetic_cancel_applied
            AND NOT profit_gate_bypassed_for_risk_exit
            AND action_execution_provenance
                = 'ORIGINAL_MAKER_ORDER_PUBLIC_PROXY_OR_SETTLEMENT'
            AND cancel_timing_provenance IS NULL
            AND self_cross_scope IS NULL
        )
    ),
    CHECK (
        action_kind <> 'FLATTEN_FOK'
        OR (
            complement_new_price_e4 IS NULL
            AND fok_book_side IS NOT NULL
            AND fok_limit_price_e4 IS NOT NULL
            AND fok_limit_fallback IS NOT NULL
            AND NOT post_only
            AND reduce_only
            AND effective_latency_ms = 60
            AND time_in_force = 'fill_or_kill'
            AND self_trade_prevention_type = 'taker_at_cross'
            AND fok_requires_prior_synthetic_cancel_applied
            AND profit_gate_bypassed_for_risk_exit
            AND action_execution_provenance
                = 'PUBLIC_BOOK_COUNTERFACTUAL'
            AND cancel_timing_provenance
                = 'SYNTHETIC_60MS_SCENARIO'
            AND self_cross_scope
                = 'SIMULATED_STRATEGY_ORDER_REGISTRY'
        )
    ),
    CHECK (legal_action),
    CHECK (skip_reason IS NULL)
);

CREATE TABLE postfill_compact_keep_transition (
    postfill_keep_transition_id VARCHAR PRIMARY KEY,
    keep_action_id VARCHAR NOT NULL,
    from_decision_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    source_date_utc DATE NOT NULL,
    market_ticker VARCHAR NOT NULL
        CHECK (market_ticker LIKE 'KXBTC15M-%'),
    data_origin VARCHAR NOT NULL CHECK (data_origin = 'PUBLIC_RAW'),
    first_fill_provenance VARCHAR NOT NULL CHECK (
        first_fill_provenance
        = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
    ),
    first_fill_execution_nature VARCHAR NOT NULL
        CHECK (first_fill_execution_nature = 'SYNTHETIC'),
    first_fill_fee_provenance VARCHAR NOT NULL
        CHECK (first_fill_fee_provenance = 'SCHEDULE_ZERO_MAKER'),
    keep_execution_provenance VARCHAR NOT NULL CHECK (
        keep_execution_provenance
        = 'ORIGINAL_MAKER_ORDER_PUBLIC_PROXY_OR_SETTLEMENT'
    ),
    from_decision_index INTEGER NOT NULL
        CHECK (from_decision_index BETWEEN 0 AND 8),
    interval_start_elapsed_ms DECIMAL(24, 6) NOT NULL
        CHECK (interval_start_elapsed_ms >= 0),
    interval_stop_elapsed_ms DECIMAL(24, 6) NOT NULL,
    market_close_elapsed_ms DECIMAL(24, 6) NOT NULL
        CHECK (market_close_elapsed_ms > 0),
    interval_start_wall_ns BIGINT NOT NULL,
    interval_start_mono_ns BIGINT NOT NULL,
    interval_stop_wall_ns BIGINT NOT NULL,
    interval_stop_mono_ns BIGINT NOT NULL,
    transition_type VARCHAR NOT NULL
        CHECK (
            transition_type IN (
                'NEXT_STATE', 'KEEP_TO_TERMINAL'
            )
        ),
    keep_terminal_type VARCHAR
        CHECK (
            keep_terminal_type IN (
                'COMPLEMENT_FILL', 'HARD_FALLBACK'
            )
        ),
    next_decision_id VARCHAR,
    first_side VARCHAR NOT NULL CHECK (first_side IN ('YES', 'NO')),
    first_fill_price_e4 INTEGER NOT NULL
        CHECK (first_fill_price_e4 BETWEEN 1 AND 9999),
    first_fill_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (first_fill_qty_fp > 0),
    first_fill_fee_usd DECIMAL(28, 12) NOT NULL
        CHECK (first_fill_fee_usd = 0),
    first_leg_cost_basis_usd DECIMAL(28, 12) NOT NULL
        CHECK (first_leg_cost_basis_usd > 0),
    original_complement_price_e4 INTEGER NOT NULL
        CHECK (original_complement_price_e4 BETWEEN 1 AND 9999),
    terminal_complement_price_e4 INTEGER
        CHECK (
            terminal_complement_price_e4 IS NULL
            OR terminal_complement_price_e4 BETWEEN 1 AND 9999
        ),
    terminal_complement_fill_qty_fp DECIMAL(28, 12)
        CHECK (
            terminal_complement_fill_qty_fp IS NULL
            OR terminal_complement_fill_qty_fp > 0
        ),
    official_market_result VARCHAR
        CHECK (official_market_result IN ('YES', 'NO')),
    official_result_provenance VARCHAR CHECK (
        official_result_provenance = 'OFFICIAL_MARKET_RESULT'
    ),
    terminal_execution_provenance VARCHAR CHECK (
        terminal_execution_provenance IN (
            'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY',
            'OFFICIAL_RESULT_SETTLEMENT_SIMULATION'
        )
    ),
    terminal_execution_nature VARCHAR
        CHECK (terminal_execution_nature = 'SYNTHETIC'),
    terminal_fee_provenance VARCHAR CHECK (
        terminal_fee_provenance IN (
            'SCHEDULE_ZERO_MAKER',
            'SETTLEMENT_ZERO_FEE'
        )
    ),
    terminal_stable_source_id VARCHAR,
    exit_fee_usd DECIMAL(28, 12) NOT NULL
        CHECK (exit_fee_usd = 0),
    immediate_gross_pnl_usd DECIMAL(28, 12) NOT NULL,
    maker_fee_usd DECIMAL(28, 12) NOT NULL CHECK (maker_fee_usd = 0),
    immediate_net_pnl_usd DECIMAL(28, 12) NOT NULL,
    simulation_reward_exact BOOLEAN NOT NULL,
    reward_value_kind VARCHAR NOT NULL
        CHECK (reward_value_kind = 'SIMULATION_EXACT'),
    capital_dollar_seconds_increment DECIMAL(38, 12) NOT NULL
        CHECK (capital_dollar_seconds_increment >= 0),
    source_rows_sha256 VARCHAR NOT NULL
        CHECK (length(source_rows_sha256) = 64),
    reconciliation_ok BOOLEAN NOT NULL,
    UNIQUE (keep_action_id),
    CHECK (source_date_utc IN (
        DATE '2026-07-20',
        DATE '2026-07-21',
        DATE '2026-07-22'
    )),
    CHECK (interval_stop_elapsed_ms > interval_start_elapsed_ms),
    CHECK (
        interval_stop_wall_ns > interval_start_wall_ns
        AND interval_stop_mono_ns > interval_start_mono_ns
    ),
    CHECK (
        first_leg_cost_basis_usd
        = CAST(
            first_fill_price_e4 * first_fill_qty_fp / 10000
            AS DECIMAL(28, 12)
          )
    ),
    CHECK (
        immediate_net_pnl_usd
        = immediate_gross_pnl_usd
    ),
    CHECK (
        capital_dollar_seconds_increment
        = CAST(
            (first_leg_cost_basis_usd + first_fill_fee_usd)
            * (interval_stop_elapsed_ms - interval_start_elapsed_ms)
            / 1000
            AS DECIMAL(38, 12)
          )
    ),
    CHECK (
        (transition_type = 'NEXT_STATE'
         AND next_decision_id IS NOT NULL
         AND keep_terminal_type IS NULL
         AND interval_stop_elapsed_ms < market_close_elapsed_ms
         AND terminal_complement_price_e4 IS NULL
         AND terminal_complement_fill_qty_fp IS NULL
         AND official_market_result IS NULL
         AND official_result_provenance IS NULL
         AND terminal_execution_provenance IS NULL
         AND terminal_execution_nature IS NULL
         AND terminal_fee_provenance IS NULL
         AND terminal_stable_source_id IS NULL
         AND immediate_gross_pnl_usd = 0
         AND exit_fee_usd = 0
         AND maker_fee_usd = 0)
        OR
        (transition_type = 'KEEP_TO_TERMINAL'
         AND next_decision_id IS NULL
         AND keep_terminal_type = 'COMPLEMENT_FILL'
         AND interval_stop_elapsed_ms < market_close_elapsed_ms
         AND terminal_complement_price_e4
             = original_complement_price_e4
         AND terminal_complement_fill_qty_fp = first_fill_qty_fp
         AND official_market_result IS NULL
         AND official_result_provenance IS NULL
         AND terminal_execution_provenance
             = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
         AND terminal_execution_nature = 'SYNTHETIC'
         AND terminal_fee_provenance = 'SCHEDULE_ZERO_MAKER'
         AND terminal_stable_source_id IS NOT NULL
         AND immediate_gross_pnl_usd
             = CAST(
                 (
                     10000 - first_fill_price_e4
                     - terminal_complement_price_e4
                   ) * first_fill_qty_fp / 10000
                 AS DECIMAL(28, 12)
               )
         AND exit_fee_usd = 0
         AND maker_fee_usd = 0)
        OR
        (transition_type = 'KEEP_TO_TERMINAL'
         AND next_decision_id IS NULL
         AND keep_terminal_type = 'HARD_FALLBACK'
         AND interval_stop_elapsed_ms = market_close_elapsed_ms
         AND terminal_complement_price_e4 IS NULL
         AND terminal_complement_fill_qty_fp IS NULL
         AND official_market_result IS NOT NULL
         AND official_result_provenance = 'OFFICIAL_MARKET_RESULT'
         AND terminal_execution_provenance
             = 'OFFICIAL_RESULT_SETTLEMENT_SIMULATION'
         AND terminal_execution_nature = 'SYNTHETIC'
         AND terminal_fee_provenance = 'SETTLEMENT_ZERO_FEE'
         AND terminal_stable_source_id IS NOT NULL
         AND immediate_gross_pnl_usd
             = CAST(
                 CASE
                   WHEN first_side = official_market_result
                   THEN (10000 - first_fill_price_e4)
                        * first_fill_qty_fp / 10000
                   ELSE -first_fill_price_e4
                        * first_fill_qty_fp / 10000
                 END
                 AS DECIMAL(28, 12)
               )
         AND exit_fee_usd = 0
         AND maker_fee_usd = 0)
    ),
    CHECK (simulation_reward_exact),
    CHECK (reconciliation_ok)
);

CREATE TABLE postfill_compact_flatten_fok_outcome (
    source_date_utc DATE NOT NULL,
    market_ticker VARCHAR NOT NULL
        CHECK (market_ticker LIKE 'KXBTC15M-%'),
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL CHECK (decision_index BETWEEN 0 AND 8),
    postfill_action_id VARCHAR PRIMARY KEY,
    data_origin VARCHAR NOT NULL CHECK (data_origin = 'PUBLIC_RAW'),
    first_fill_provenance VARCHAR NOT NULL CHECK (
        first_fill_provenance
        = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
    ),
    first_fill_execution_nature VARCHAR NOT NULL
        CHECK (first_fill_execution_nature = 'SYNTHETIC'),
    first_fill_fee_provenance VARCHAR NOT NULL
        CHECK (first_fill_fee_provenance = 'SCHEDULE_ZERO_MAKER'),
    fok_execution_provenance VARCHAR NOT NULL CHECK (
        fok_execution_provenance = 'PUBLIC_BOOK_COUNTERFACTUAL'
    ),
    cancel_timing_provenance VARCHAR NOT NULL CHECK (
        cancel_timing_provenance = 'SYNTHETIC_60MS_SCENARIO'
    ),
    self_cross_scope VARCHAR NOT NULL CHECK (
        self_cross_scope = 'SIMULATED_STRATEGY_ORDER_REGISTRY'
    ),
    terminal_type VARCHAR NOT NULL
        CHECK (
            terminal_type IN (
                'CANCEL_RACE_PAIR', 'FOK_FULL', 'FOK_ZERO'
            )
        ),
    planned_effective_elapsed_ms BIGINT NOT NULL,
    terminal_elapsed_ms DECIMAL(24, 6) NOT NULL,
    market_close_elapsed_ms DECIMAL(24, 6) NOT NULL
        CHECK (market_close_elapsed_ms > 0),
    terminal_wall_ns BIGINT NOT NULL CHECK (terminal_wall_ns > 0),
    terminal_mono_ns BIGINT NOT NULL CHECK (terminal_mono_ns > 0),
    synthetic_cancel_applied_wall_ns BIGINT,
    synthetic_cancel_applied_mono_ns BIGINT,
    synthetic_cancel_applied_sequence INTEGER,
    synthetic_cancel_applied_stable_source_id VARCHAR,
    fok_processed_wall_ns BIGINT,
    fok_processed_mono_ns BIGINT,
    fok_processed_sequence INTEGER,
    fok_processed_stable_source_id VARCHAR,
    feature_asof_wall_ns BIGINT NOT NULL,
    source_max_recv_wall_ns BIGINT NOT NULL,
    source_max_recv_mono_ns BIGINT NOT NULL,
    source_max_stable_id VARCHAR NOT NULL,
    outcome_source_rows_sha256 VARCHAR NOT NULL
        CHECK (length(outcome_source_rows_sha256) = 64),
    fok_sent BOOLEAN NOT NULL,
    synthetic_cancel_applied_before_terminal BOOLEAN NOT NULL,
    public_crossing_event_applied BOOLEAN NOT NULL,
    simulated_no_self_cross_verified_before_fok BOOLEAN,
    simulated_strategy_order_registry_sha256 VARCHAR NOT NULL
        CHECK (
            length(simulated_strategy_order_registry_sha256) = 64
        ),
    fok_book_side VARCHAR NOT NULL
        CHECK (fok_book_side IN ('BID', 'ASK')),
    fok_limit_price_e4 INTEGER NOT NULL
        CHECK (fok_limit_price_e4 BETWEEN 10 AND 9990),
    fok_limit_fallback BOOLEAN NOT NULL,
    first_fill_price_e4 INTEGER NOT NULL
        CHECK (first_fill_price_e4 BETWEEN 1 AND 9999),
    first_fill_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (first_fill_qty_fp > 0),
    first_fill_fee_usd DECIMAL(28, 12) NOT NULL
        CHECK (first_fill_fee_usd = 0),
    first_leg_cost_basis_usd DECIMAL(28, 12) NOT NULL
        CHECK (first_leg_cost_basis_usd > 0),
    original_complement_price_e4 INTEGER NOT NULL
        CHECK (original_complement_price_e4 BETWEEN 1 AND 9999),
    requested_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (requested_qty_fp > 0),
    complement_fill_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (complement_fill_qty_fp >= 0),
    fok_fill_qty_fp DECIMAL(28, 12) NOT NULL
        CHECK (fok_fill_qty_fp >= 0),
    residual_inventory_fp DECIMAL(28, 12) NOT NULL
        CHECK (residual_inventory_fp >= 0),
    gross_pnl_usd DECIMAL(28, 12) NOT NULL,
    maker_fee_usd DECIMAL(28, 12) NOT NULL CHECK (maker_fee_usd = 0),
    taker_fee_low_usd DECIMAL(28, 12) NOT NULL
        CHECK (taker_fee_low_usd >= 0),
    taker_fee_base_usd DECIMAL(28, 12) NOT NULL
        CHECK (taker_fee_base_usd >= 0),
    taker_fee_high_usd DECIMAL(28, 12) NOT NULL
        CHECK (taker_fee_high_usd >= 0),
    net_pnl_fee_low_usd DECIMAL(28, 12) NOT NULL,
    net_pnl_fee_base_usd DECIMAL(28, 12) NOT NULL,
    net_pnl_fee_high_usd DECIMAL(28, 12) NOT NULL,
    conservative_reward_usd DECIMAL(28, 12) NOT NULL,
    simulation_reward_exact BOOLEAN NOT NULL,
    reward_value_kind VARCHAR NOT NULL
        CHECK (
            reward_value_kind IN (
                'SIMULATION_EXACT', 'FEE_BAND', 'LOWER_BOUND'
            )
        ),
    conservative_fit_usable BOOLEAN NOT NULL,
    capital_dollar_seconds DECIMAL(38, 12) NOT NULL
        CHECK (capital_dollar_seconds >= 0),
    capital_released BOOLEAN NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    data_invalid BOOLEAN NOT NULL,
    execution_slices_sha256 VARCHAR NOT NULL
        CHECK (length(execution_slices_sha256) = 64),
    race_complement_price_e4 INTEGER,
    race_complement_fee_usd DECIMAL(28, 12),
    race_complement_stable_source_id VARCHAR,
    race_fill_provenance VARCHAR CHECK (
        race_fill_provenance
        = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
    ),
    race_execution_nature VARCHAR
        CHECK (race_execution_nature = 'SYNTHETIC'),
    race_fee_provenance VARCHAR
        CHECK (race_fee_provenance = 'SCHEDULE_ZERO_MAKER'),
    UNIQUE (postfill_episode_id, decision_index),
    CHECK (source_date_utc IN (
        DATE '2026-07-20',
        DATE '2026-07-21',
        DATE '2026-07-22'
    )),
    CHECK (
        planned_effective_elapsed_ms >= 60
        AND terminal_elapsed_ms > planned_effective_elapsed_ms - 60
        AND terminal_elapsed_ms <= market_close_elapsed_ms
    ),
    CHECK (
        requested_qty_fp = first_fill_qty_fp
        AND first_leg_cost_basis_usd
            = CAST(
                first_fill_price_e4 * first_fill_qty_fp / 10000
                AS DECIMAL(28, 12)
              )
    ),
    CHECK (
        complement_fill_qty_fp + fok_fill_qty_fp
        + residual_inventory_fp = requested_qty_fp
    ),
    CHECK (
        net_pnl_fee_low_usd
        = gross_pnl_usd - maker_fee_usd - taker_fee_low_usd
        AND net_pnl_fee_base_usd
        = gross_pnl_usd - maker_fee_usd - taker_fee_base_usd
        AND net_pnl_fee_high_usd
        = gross_pnl_usd - maker_fee_usd - taker_fee_high_usd
    ),
    CHECK (
        taker_fee_low_usd <= taker_fee_base_usd
        AND taker_fee_base_usd <= taker_fee_high_usd
        AND net_pnl_fee_low_usd >= net_pnl_fee_base_usd
        AND net_pnl_fee_base_usd >= net_pnl_fee_high_usd
    ),
    CHECK (NOT public_crossing_event_applied),
    CHECK (
        terminal_type <> 'CANCEL_RACE_PAIR'
        OR (
            NOT synthetic_cancel_applied_before_terminal
            AND NOT fok_sent
            AND simulated_no_self_cross_verified_before_fok IS NULL
            AND synthetic_cancel_applied_wall_ns IS NULL
            AND synthetic_cancel_applied_mono_ns IS NULL
            AND synthetic_cancel_applied_sequence IS NULL
            AND synthetic_cancel_applied_stable_source_id IS NULL
            AND fok_processed_wall_ns IS NULL
            AND fok_processed_mono_ns IS NULL
            AND fok_processed_sequence IS NULL
            AND fok_processed_stable_source_id IS NULL
            AND terminal_elapsed_ms
                > planned_effective_elapsed_ms - 60
            AND terminal_elapsed_ms < planned_effective_elapsed_ms
            AND complement_fill_qty_fp = requested_qty_fp
            AND fok_fill_qty_fp = 0
            AND residual_inventory_fp = 0
            AND taker_fee_low_usd = 0
            AND taker_fee_base_usd = 0
            AND taker_fee_high_usd = 0
            AND maker_fee_usd = 0
            AND gross_pnl_usd
                = CAST(
                    (
                        10000 - first_fill_price_e4
                        - race_complement_price_e4
                      ) * requested_qty_fp / 10000
                    AS DECIMAL(28, 12)
                  )
            AND conservative_reward_usd = net_pnl_fee_high_usd
            AND simulation_reward_exact
            AND reward_value_kind = 'SIMULATION_EXACT'
            AND conservative_fit_usable
            AND capital_released
            AND race_complement_price_e4
                = original_complement_price_e4
            AND race_complement_fee_usd = 0
            AND race_complement_stable_source_id IS NOT NULL
            AND race_fill_provenance
                = 'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
            AND race_execution_nature = 'SYNTHETIC'
            AND race_fee_provenance = 'SCHEDULE_ZERO_MAKER'
            AND capital_dollar_seconds
                = CAST(
                    (first_leg_cost_basis_usd + first_fill_fee_usd)
                    * terminal_elapsed_ms / 1000
                    AS DECIMAL(38, 12)
                  )
        )
    ),
    CHECK (
        terminal_type NOT IN ('FOK_FULL', 'FOK_ZERO')
        OR (
            synthetic_cancel_applied_before_terminal
            AND fok_sent
            AND simulated_no_self_cross_verified_before_fok
            AND terminal_elapsed_ms = planned_effective_elapsed_ms
            AND synthetic_cancel_applied_wall_ns = terminal_wall_ns
            AND synthetic_cancel_applied_mono_ns = terminal_mono_ns
            AND synthetic_cancel_applied_sequence = 0
            AND synthetic_cancel_applied_stable_source_id IS NOT NULL
            AND fok_processed_wall_ns IS NOT NULL
            AND fok_processed_mono_ns IS NOT NULL
            AND fok_processed_wall_ns = terminal_wall_ns
            AND fok_processed_mono_ns = terminal_mono_ns
            AND fok_processed_sequence = 1
            AND fok_processed_stable_source_id IS NOT NULL
            AND race_complement_price_e4 IS NULL
            AND race_complement_fee_usd IS NULL
            AND race_complement_stable_source_id IS NULL
            AND race_fill_provenance IS NULL
            AND race_execution_nature IS NULL
            AND race_fee_provenance IS NULL
            AND maker_fee_usd = 0
        )
    ),
    CHECK (
        terminal_type <> 'FOK_FULL'
        OR (
            complement_fill_qty_fp = 0
            AND fok_fill_qty_fp = requested_qty_fp
            AND residual_inventory_fp = 0
            AND NOT simulation_reward_exact
            AND reward_value_kind = 'FEE_BAND'
            AND conservative_fit_usable
            AND capital_released
            AND conservative_reward_usd = net_pnl_fee_high_usd
            AND capital_dollar_seconds
                = CAST(
                    (first_leg_cost_basis_usd + first_fill_fee_usd)
                    * terminal_elapsed_ms / 1000
                    AS DECIMAL(38, 12)
                  )
        )
    ),
    CHECK (
        terminal_type <> 'FOK_ZERO'
        OR (
            complement_fill_qty_fp = 0
            AND fok_fill_qty_fp = 0
            AND residual_inventory_fp = requested_qty_fp
            AND gross_pnl_usd = 0
            AND taker_fee_low_usd = 0
            AND taker_fee_base_usd = 0
            AND taker_fee_high_usd = 0
            AND maker_fee_usd = first_fill_fee_usd
            AND conservative_reward_usd
                = -(first_leg_cost_basis_usd + first_fill_fee_usd)
            AND NOT simulation_reward_exact
            AND reward_value_kind = 'LOWER_BOUND'
            AND conservative_fit_usable
            AND NOT capital_released
            AND capital_dollar_seconds
                = CAST(
                    (first_leg_cost_basis_usd + first_fill_fee_usd)
                    * market_close_elapsed_ms / 1000
                    AS DECIMAL(38, 12)
                  )
        )
    ),
    CHECK (reconciliation_ok),
    CHECK (NOT data_invalid)
);
