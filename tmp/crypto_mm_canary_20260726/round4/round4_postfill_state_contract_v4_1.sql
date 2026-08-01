-- ROUND4 Stage-2 V4.1 independent normalized schema.
--
-- This file does not reuse the legacy ROUND4 zero-atom table or any V4
-- nullable-provenance branch.  Cross-row economics are commit-authorized
-- only by round4_postfill_v4_1_validator.sql through the table-builder
-- transaction gate.

CREATE TABLE postfill_v41_fee_schedule_receipt (
    fee_schedule_receipt_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    series_ticker VARCHAR NOT NULL,
    schedule_effective_date_utc VARCHAR NOT NULL,
    schedule_pdf_url VARCHAR NOT NULL,
    schedule_pdf_local_path VARCHAR NOT NULL,
    schedule_pdf_local_size_bytes BIGINT NOT NULL,
    schedule_pdf_local_modified_at VARCHAR NOT NULL,
    schedule_pdf_where_froms VARCHAR NOT NULL,
    schedule_pdf_where_froms_xattr_sha256 VARCHAR NOT NULL,
    schedule_pdf_page_count INTEGER NOT NULL,
    schedule_pdf_raw_sha256 VARCHAR NOT NULL,
    schedule_pdf_raw_bytes_authenticated BOOLEAN NOT NULL,
    schedule_pdf_normalized_content_sha256 VARCHAR NOT NULL,
    schedule_pdf_normalized_content_provenance VARCHAR NOT NULL,
    schedule_pdf_normalized_content_char_count INTEGER NOT NULL,
    series_get_captured_at_utc VARCHAR NOT NULL,
    series_get_raw_sha256 VARCHAR NOT NULL,
    series_fee_changes_captured_at_utc VARCHAR NOT NULL,
    series_fee_changes_raw_sha256 VARCHAR NOT NULL,
    series_fee_changes_show_historical BOOLEAN NOT NULL,
    series_fee_change_count INTEGER NOT NULL,
    series_fee_type VARCHAR NOT NULL,
    series_fee_multiplier DECIMAL(38, 12) NOT NULL,
    series_last_updated_ts VARCHAR NOT NULL,
    general_taker_rate DECIMAL(38, 12) NOT NULL,
    general_maker_rate DECIMAL(38, 12) NOT NULL,
    general_maker_default_multiplier DECIMAL(38, 12) NOT NULL,
    series_maker_multiplier DECIMAL(38, 12) NOT NULL,
    series_taker_multiplier DECIMAL(38, 12) NOT NULL,
    settlement_fee_usd DECIMAL(38, 12) NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        fee_schedule_receipt_id =
            'KXBTC15M_FEE_SCHEDULE_2026_07_07'
        AND series_ticker = 'KXBTC15M'
        AND schedule_effective_date_utc = '2026-07-07'
        AND schedule_pdf_url =
            'https://kalshi.com/docs/kalshi-fee-schedule.pdf'
        AND schedule_pdf_local_path =
            '/Applications/Research Ritch/kalshi-fee-schedule July''.pdf'
        AND schedule_pdf_local_size_bytes = 382507
        AND schedule_pdf_local_modified_at =
            '2026-07-24T22:57:50-04:00'
        AND schedule_pdf_where_froms =
            '["https://kalshi.com/docs/kalshi-fee-schedule.pdf","https://kalshi.com/docs/kalshi-fee-schedule.pdf"]'
        AND schedule_pdf_where_froms_xattr_sha256 =
            '1f0c5b6d7b9718fbe402ee43501b9aa649b97a69abba972f1639fb3c71fceaee'
        AND schedule_pdf_page_count = 12
        AND schedule_pdf_raw_sha256 =
            '815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c'
        AND schedule_pdf_raw_bytes_authenticated
        AND schedule_pdf_normalized_content_sha256 =
            '5f90733a0dae6e3d9577efb37895011ddda96385731481a7706c48643b58276d'
        AND schedule_pdf_normalized_content_provenance =
            'LOCAL_AUTHENTICATED_PDF_PYMUPDF_PAGE_TEXT_JOIN_LF'
        AND schedule_pdf_normalized_content_char_count = 9612
    ),
    CHECK (
        series_get_captured_at_utc = '2026-07-26T11:43:46Z'
        AND series_get_raw_sha256 =
            'be24516ae4825de53af89b12ead76e3a93ac52e18ef67bf519621adde3ed5bc2'
        AND series_fee_changes_captured_at_utc =
            '2026-07-26T11:43:51Z'
        AND series_fee_changes_raw_sha256 =
            '6780c8eb7edbb5e1ca3b15166f17cef054befacb2cc8f4bc479c54074a9a2c18'
        AND series_fee_changes_show_historical
        AND series_fee_change_count = 0
        AND series_fee_type = 'quadratic'
        AND series_fee_multiplier = 1
        AND series_last_updated_ts = '2026-07-01T18:05:04.527379Z'
    ),
    CHECK (
        general_taker_rate = 0.07
        AND general_maker_rate = 0.0175
        AND general_maker_default_multiplier = 0
        AND series_maker_multiplier = 0
        AND series_taker_multiplier = 1
        AND settlement_fee_usd = 0
        AND source_rows_sha256 =
            '477dea5d3fef250f1f92d1273b1775b97db30cc80217d22b6504e557f3f7a17a'
    )
);

CREATE TABLE postfill_v41_market_metadata_receipt (
    market_metadata_receipt_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_window_open_wall_ns BIGINT NOT NULL,
    market_window_close_wall_ns BIGINT NOT NULL,
    price_level_structure VARCHAR NOT NULL,
    market_min_fill_increment_fp DECIMAL(38, 12) NOT NULL,
    market_fill_increment_guarantees_true_fill_min BOOLEAN NOT NULL,
    market_metadata_stable_source_id VARCHAR NOT NULL,
    provenance VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    UNIQUE (market_ticker, market_id, source_date_utc),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        market_metadata_receipt_id = market_metadata_stable_source_id
        AND market_window_open_wall_ns < market_window_close_wall_ns
        AND price_level_structure = 'tapered_deci_cent'
        AND market_min_fill_increment_fp = 0.01
        AND market_fill_increment_guarantees_true_fill_min
        AND provenance = 'PUBLIC_MARKET_METADATA'
    )
);

CREATE TABLE postfill_v41_settlement_receipt (
    settlement_receipt_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    official_market_result VARCHAR NOT NULL,
    market_status VARCHAR NOT NULL,
    settlement_recv_wall_ns BIGINT NOT NULL,
    settlement_recv_mono_ns BIGINT NOT NULL,
    settlement_ingest_sequence BIGINT NOT NULL,
    settlement_stable_source_id VARCHAR NOT NULL,
    determined_wall_ns BIGINT NOT NULL,
    finalized_wall_ns BIGINT NOT NULL,
    provenance VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    UNIQUE (market_ticker, market_id, source_date_utc),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        settlement_receipt_id = settlement_stable_source_id
        AND official_market_result IN ('YES', 'NO')
        AND market_status = 'FINALIZED'
        AND provenance = 'PUBLIC_FINALIZED_MARKET_RECEIPT'
        AND determined_wall_ns <= finalized_wall_ns
        AND finalized_wall_ns <= settlement_recv_wall_ns
    )
);

CREATE TABLE postfill_v41_causal_state (
    postfill_decision_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    action_family_version VARCHAR NOT NULL,
    experiment_id VARCHAR NOT NULL,
    data_role VARCHAR NOT NULL,
    data_origin VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    market_metadata_receipt_id VARCHAR NOT NULL,
    market_metadata_source_rows_sha256 VARCHAR NOT NULL,
    fee_schedule_receipt_id VARCHAR NOT NULL,
    fee_schedule_source_rows_sha256 VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    entry_episode_id VARCHAR NOT NULL,
    entry_action_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL,
    decision_elapsed_ms DECIMAL(38, 12) NOT NULL,
    decision_recv_wall_ns BIGINT NOT NULL,
    decision_recv_mono_ns BIGINT NOT NULL,
    source_max_recv_wall_ns BIGINT NOT NULL,
    source_max_recv_mono_ns BIGINT NOT NULL,
    source_max_ingest_sequence BIGINT NOT NULL,
    source_max_stable_id VARCHAR NOT NULL,
    causal_source_rows_sha256 VARCHAR NOT NULL,
    paired_state_fingerprint_sha256 VARCHAR NOT NULL,
    simulated_cancel_state VARCHAR NOT NULL,
    first_side VARCHAR NOT NULL,
    first_price_e4 INTEGER NOT NULL,
    first_qty_fp DECIMAL(38, 12) NOT NULL,
    first_maker_trade_fee_usd DECIMAL(38, 12) NOT NULL,
    first_maker_net_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    first_leg_principal_usd DECIMAL(38, 12) NOT NULL,
    first_leg_basis_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    complement_side VARCHAR NOT NULL,
    complement_price_e4 INTEGER NOT NULL,
    complement_maker_trade_fee_usd DECIMAL(38, 12) NOT NULL,
    complement_maker_net_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    complement_principal_usd DECIMAL(38, 12) NOT NULL,
    complement_reservation_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    locked_pair_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    market_close_elapsed_ms DECIMAL(38, 12) NOT NULL,
    flatten_book_side VARCHAR NOT NULL,
    flatten_limit_price_e4 INTEGER NOT NULL,
    flatten_limit_fallback BOOLEAN NOT NULL,
    flatten_adverse_tick_e4 INTEGER NOT NULL,
    flatten_visible_slices_sha256 VARCHAR NOT NULL,
    market_min_fill_increment_fp DECIMAL(38, 12) NOT NULL,
    trade_fee_quantum_usd DECIMAL(38, 12) NOT NULL,
    account_rounding_target_usd DECIMAL(38, 12) NOT NULL,
    target_account_class VARCHAR NOT NULL,
    account_precision_receipt_sha256 VARCHAR NOT NULL,
    fok_fee_n_max INTEGER NOT NULL,
    fok_required_principal_usd DECIMAL(38, 12) NOT NULL,
    fok_expected_sale_proceeds_usd DECIMAL(38, 12) NOT NULL,
    fok_required_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    simulated_available_cash_before_cancel_usd DECIMAL(38, 12) NOT NULL,
    simulated_available_cash_after_cancel_usd DECIMAL(38, 12) NOT NULL,
    fok_required_cash_with_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    fok_candidate_reserve_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    peak_strategy_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    fok_cash_fee_feasible BOOLEAN NOT NULL,
    fee_bound_selected_for_candidate VARCHAR NOT NULL,
    fee_accumulator_receipt_authenticated BOOLEAN NOT NULL,
    simulated_strategy_order_registry_sha256 VARCHAR NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    FOREIGN KEY (market_metadata_receipt_id)
        REFERENCES postfill_v41_market_metadata_receipt(
            market_metadata_receipt_id
        ),
    FOREIGN KEY (fee_schedule_receipt_id)
        REFERENCES postfill_v41_fee_schedule_receipt(
            fee_schedule_receipt_id
        ),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        action_family_version =
        'ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_1'
    ),
    CHECK (data_role = 'DISCOVERY' AND data_origin = 'PUBLIC_RAW'),
    CHECK (source_date_utc IN ('2026-07-20', '2026-07-21', '2026-07-22')),
    CHECK (simulated_cancel_state = 'NONE'),
    CHECK (first_side IN ('YES', 'NO')),
    CHECK (
        (first_side = 'YES' AND complement_side = 'NO')
        OR (first_side = 'NO' AND complement_side = 'YES')
    ),
    CHECK (first_qty_fp > 0 AND first_qty_fp % 0.01 = 0),
    CHECK (
        first_price_e4 BETWEEN 10 AND 9990
        AND (
            (
                (first_price_e4 < 1000 OR first_price_e4 > 9000)
                AND first_price_e4 % 10 = 0
            )
            OR (
                first_price_e4 BETWEEN 1000 AND 9000
                AND first_price_e4 % 100 = 0
            )
        )
        AND first_price_e4 % 100 = 0
    ),
    CHECK (
        complement_price_e4 BETWEEN 10 AND 9990
        AND (
            (
                (complement_price_e4 < 1000 OR complement_price_e4 > 9000)
                AND complement_price_e4 % 10 = 0
            )
            OR (
                complement_price_e4 BETWEEN 1000 AND 9000
                AND complement_price_e4 % 100 = 0
            )
        )
        AND complement_price_e4 % 100 = 0
    ),
    CHECK (
        flatten_limit_price_e4 BETWEEN 10 AND 9990
        AND (
            (
                (flatten_limit_price_e4 < 1000
                 OR flatten_limit_price_e4 > 9000)
                AND flatten_limit_price_e4 % 10 = 0
            )
            OR (
                flatten_limit_price_e4 BETWEEN 1000 AND 9000
                AND flatten_limit_price_e4 % 100 = 0
            )
        )
    ),
    CHECK (
        flatten_book_side =
        CASE WHEN first_side = 'YES' THEN 'ASK' ELSE 'BID' END
    ),
    CHECK (first_maker_trade_fee_usd = 0),
    CHECK (complement_maker_trade_fee_usd = 0),
    CHECK (
        first_leg_principal_usd =
        first_qty_fp * first_price_e4 / 10000
    ),
    CHECK (
        first_leg_basis_safe_upper_usd =
        first_leg_principal_usd + first_maker_net_fee_safe_upper_usd
    ),
    CHECK (
        complement_principal_usd =
        first_qty_fp * complement_price_e4 / 10000
    ),
    CHECK (
        complement_reservation_safe_upper_usd =
        complement_principal_usd
        + complement_maker_net_fee_safe_upper_usd
    ),
    CHECK (
        locked_pair_capital_safe_upper_usd =
        first_leg_basis_safe_upper_usd
        + complement_reservation_safe_upper_usd
    ),
    CHECK (
        market_min_fill_increment_fp = 0.01
        AND trade_fee_quantum_usd = 0.0001
        AND account_rounding_target_usd = 0.0001
        AND target_account_class = 'DIRECT'
        AND account_precision_receipt_sha256 =
            '816528a20ee70ffc7536c02b8984a69f8730f8304656a259e2edd124e6db8da6'
    ),
    CHECK (fok_fee_n_max = CEIL(first_qty_fp / 0.01)),
    CHECK (
        simulated_available_cash_after_cancel_usd =
        simulated_available_cash_before_cancel_usd
        + complement_reservation_safe_upper_usd
    ),
    CHECK (
        (flatten_book_side = 'BID'
         AND fok_required_principal_usd =
             first_qty_fp * flatten_limit_price_e4 / 10000
         AND fok_expected_sale_proceeds_usd = 0
         AND fok_required_cash_with_fee_safe_upper_usd =
             fok_required_principal_usd
             + fok_required_fee_safe_upper_usd)
        OR
        (flatten_book_side = 'ASK'
         AND fok_required_principal_usd = 0
         AND fok_expected_sale_proceeds_usd =
             first_qty_fp * flatten_limit_price_e4 / 10000
         AND fok_required_cash_with_fee_safe_upper_usd =
             GREATEST(
                 0,
                 fok_required_fee_safe_upper_usd
                 - fok_expected_sale_proceeds_usd
             ))
    ),
    CHECK (
        fok_candidate_reserve_safe_upper_usd =
        fok_required_cash_with_fee_safe_upper_usd
    ),
    CHECK (
        peak_strategy_capital_safe_upper_usd =
        GREATEST(
            locked_pair_capital_safe_upper_usd,
            first_leg_basis_safe_upper_usd
            + fok_candidate_reserve_safe_upper_usd
        )
    ),
    CHECK (
        fok_cash_fee_feasible
        AND simulated_available_cash_after_cancel_usd >=
            fok_required_cash_with_fee_safe_upper_usd
    ),
    CHECK (
        fee_bound_selected_for_candidate =
        'PARTITION_DP_NO_REBATE_SAFE_UPPER'
        AND NOT fee_accumulator_receipt_authenticated
    ),
    CHECK (
        source_max_recv_wall_ns <= decision_recv_wall_ns
        AND source_max_recv_mono_ns <= decision_recv_mono_ns
    ),
    CHECK (reconciliation_ok)
);

CREATE TABLE postfill_v41_action (
    postfill_action_id VARCHAR PRIMARY KEY,
    postfill_decision_id VARCHAR NOT NULL,
    contract_version VARCHAR NOT NULL,
    action_family_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL,
    action_kind VARCHAR NOT NULL,
    paired_state_fingerprint_sha256 VARCHAR NOT NULL,
    requested_qty_fp DECIMAL(38, 12) NOT NULL,
    complement_old_price_e4 INTEGER NOT NULL,
    fok_book_side VARCHAR,
    fok_limit_price_e4 INTEGER,
    fok_limit_fallback BOOLEAN,
    effective_latency_ms DECIMAL(38, 12) NOT NULL,
    time_in_force VARCHAR,
    self_trade_prevention_type VARCHAR,
    fok_requires_prior_synthetic_cancel_applied BOOLEAN NOT NULL,
    simulated_cancel_state_before_action VARCHAR NOT NULL,
    complement_reservation_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    locked_pair_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    simulated_available_cash_before_cancel_usd DECIMAL(38, 12) NOT NULL,
    simulated_available_cash_after_cancel_usd DECIMAL(38, 12) NOT NULL,
    fok_required_principal_usd DECIMAL(38, 12) NOT NULL,
    fok_expected_sale_proceeds_usd DECIMAL(38, 12) NOT NULL,
    fok_required_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    fok_required_cash_with_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    fok_candidate_reserve_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    peak_strategy_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    fok_cash_fee_feasible BOOLEAN NOT NULL,
    fee_bound_selected_for_candidate VARCHAR NOT NULL,
    legal_action BOOLEAN NOT NULL,
    FOREIGN KEY (postfill_decision_id)
        REFERENCES postfill_v41_causal_state(postfill_decision_id),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        action_family_version =
        'ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_1'
    ),
    CHECK (action_kind IN ('KEEP', 'FLATTEN_FOK')),
    CHECK (simulated_cancel_state_before_action = 'NONE'),
    CHECK (
        fee_bound_selected_for_candidate =
        'PARTITION_DP_NO_REBATE_SAFE_UPPER'
    ),
    CHECK (legal_action AND fok_cash_fee_feasible),
    CHECK (
        (action_kind = 'KEEP'
         AND effective_latency_ms = 0
         AND time_in_force IS NULL
         AND self_trade_prevention_type IS NULL
         AND NOT fok_requires_prior_synthetic_cancel_applied
         AND fok_book_side IS NULL
         AND fok_limit_price_e4 IS NULL
         AND fok_limit_fallback IS NULL
         AND fok_required_principal_usd = 0
         AND fok_expected_sale_proceeds_usd = 0
         AND fok_required_fee_safe_upper_usd = 0
         AND fok_required_cash_with_fee_safe_upper_usd = 0
         AND fok_candidate_reserve_safe_upper_usd = 0)
        OR
        (action_kind = 'FLATTEN_FOK'
         AND effective_latency_ms = 60
         AND time_in_force = 'fill_or_kill'
         AND self_trade_prevention_type = 'taker_at_cross'
         AND fok_requires_prior_synthetic_cancel_applied
         AND fok_book_side IN ('ASK', 'BID')
         AND fok_limit_price_e4 IS NOT NULL
         AND fok_limit_fallback IS NOT NULL
         AND fok_candidate_reserve_safe_upper_usd =
             fok_required_cash_with_fee_safe_upper_usd)
    )
);

CREATE TABLE postfill_v41_keep_transition (
    postfill_keep_transition_id VARCHAR PRIMARY KEY,
    keep_action_id VARCHAR NOT NULL,
    from_decision_id VARCHAR NOT NULL,
    contract_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    from_decision_index INTEGER NOT NULL,
    interval_start_elapsed_ms DECIMAL(38, 12) NOT NULL,
    interval_stop_elapsed_ms DECIMAL(38, 12) NOT NULL,
    interval_start_wall_ns BIGINT NOT NULL,
    interval_start_mono_ns BIGINT NOT NULL,
    interval_stop_wall_ns BIGINT NOT NULL,
    interval_stop_mono_ns BIGINT NOT NULL,
    transition_type VARCHAR NOT NULL,
    keep_terminal_type VARCHAR,
    next_decision_id VARCHAR,
    terminal_public_evidence_id VARCHAR,
    market_close_elapsed_ms DECIMAL(38, 12) NOT NULL,
    complement_release_elapsed_ms DECIMAL(38, 12) NOT NULL,
    settlement_release_elapsed_ms DECIMAL(38, 12) NOT NULL,
    market_metadata_receipt_id VARCHAR NOT NULL,
    fee_schedule_receipt_id VARCHAR NOT NULL,
    public_settlement_receipt_id VARCHAR NOT NULL,
    public_settlement_recv_wall_ns BIGINT NOT NULL,
    public_settlement_recv_mono_ns BIGINT NOT NULL,
    public_settlement_status VARCHAR NOT NULL,
    official_market_result VARCHAR NOT NULL,
    public_settlement_source_rows_sha256 VARCHAR NOT NULL,
    public_settlement_provenance VARCHAR NOT NULL,
    first_side VARCHAR NOT NULL,
    first_price_e4 INTEGER NOT NULL,
    first_qty_fp DECIMAL(38, 12) NOT NULL,
    first_leg_basis_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    complement_reservation_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    locked_pair_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    immediate_gross_pnl_usd DECIMAL(38, 12) NOT NULL,
    maker_net_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    conservative_net_pnl_usd DECIMAL(38, 12) NOT NULL,
    fee_bound_selected_for_candidate VARCHAR NOT NULL,
    capital_dollar_seconds_increment DECIMAL(38, 12) NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    FOREIGN KEY (keep_action_id)
        REFERENCES postfill_v41_action(postfill_action_id),
    FOREIGN KEY (from_decision_id)
        REFERENCES postfill_v41_causal_state(postfill_decision_id),
    FOREIGN KEY (next_decision_id)
        REFERENCES postfill_v41_causal_state(postfill_decision_id),
    FOREIGN KEY (market_metadata_receipt_id)
        REFERENCES postfill_v41_market_metadata_receipt(
            market_metadata_receipt_id
        ),
    FOREIGN KEY (fee_schedule_receipt_id)
        REFERENCES postfill_v41_fee_schedule_receipt(
            fee_schedule_receipt_id
        ),
    FOREIGN KEY (public_settlement_receipt_id)
        REFERENCES postfill_v41_settlement_receipt(
            settlement_receipt_id
        ),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (interval_start_elapsed_ms < interval_stop_elapsed_ms),
    CHECK (
        interval_start_wall_ns < interval_stop_wall_ns
        AND interval_start_mono_ns < interval_stop_mono_ns
    ),
    CHECK (
        transition_type IN ('NEXT_STATE', 'KEEP_TO_TERMINAL')
    ),
    CHECK (
        (transition_type = 'NEXT_STATE'
         AND keep_terminal_type IS NULL
         AND next_decision_id IS NOT NULL
         AND terminal_public_evidence_id IS NULL)
        OR
        (transition_type = 'KEEP_TO_TERMINAL'
         AND keep_terminal_type IN ('COMPLEMENT_FILL', 'HARD_FALLBACK')
         AND next_decision_id IS NULL
         AND terminal_public_evidence_id IS NOT NULL)
    ),
    CHECK (public_settlement_status = 'FINALIZED'),
    CHECK (official_market_result IN ('YES', 'NO')),
    CHECK (
        public_settlement_provenance =
        'PUBLIC_SETTLEMENT_RECEIPT_SIMULATION'
    ),
    CHECK (
        settlement_release_elapsed_ms >= market_close_elapsed_ms
        AND complement_release_elapsed_ms <=
            settlement_release_elapsed_ms
    ),
    CHECK (
        locked_pair_capital_safe_upper_usd =
        first_leg_basis_safe_upper_usd
        + complement_reservation_safe_upper_usd
    ),
    CHECK (
        conservative_net_pnl_usd =
        immediate_gross_pnl_usd - maker_net_fee_safe_upper_usd
    ),
    CHECK (
        capital_dollar_seconds_increment =
        CASE
            WHEN interval_stop_elapsed_ms <=
                 complement_release_elapsed_ms
            THEN locked_pair_capital_safe_upper_usd
                 * (interval_stop_elapsed_ms
                    - interval_start_elapsed_ms) / 1000
            WHEN interval_start_elapsed_ms >=
                 complement_release_elapsed_ms
            THEN first_leg_basis_safe_upper_usd
                 * (interval_stop_elapsed_ms
                    - interval_start_elapsed_ms) / 1000
            ELSE (
                locked_pair_capital_safe_upper_usd
                * (complement_release_elapsed_ms
                   - interval_start_elapsed_ms)
                + first_leg_basis_safe_upper_usd
                * (interval_stop_elapsed_ms
                   - complement_release_elapsed_ms)
            ) / 1000
        END
    ),
    CHECK (
        fee_bound_selected_for_candidate =
        'PARTITION_DP_NO_REBATE_SAFE_UPPER'
        AND reconciliation_ok
    )
);

CREATE TABLE postfill_v41_flatten_fok_outcome (
    postfill_fok_outcome_id VARCHAR PRIMARY KEY,
    postfill_action_id VARCHAR NOT NULL,
    contract_version VARCHAR NOT NULL,
    action_family_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL,
    terminal_type VARCHAR NOT NULL,
    planned_effective_elapsed_ms DECIMAL(38, 12) NOT NULL,
    terminal_elapsed_ms DECIMAL(38, 12) NOT NULL,
    synthetic_cancel_applied_wall_ns BIGINT,
    synthetic_cancel_applied_mono_ns BIGINT,
    synthetic_cancel_applied_sequence INTEGER,
    synthetic_cancel_applied_stable_source_id VARCHAR,
    fok_processed_wall_ns BIGINT,
    fok_processed_mono_ns BIGINT,
    fok_processed_sequence INTEGER,
    fok_processed_stable_source_id VARCHAR,
    fok_sent BOOLEAN NOT NULL,
    simulated_no_self_cross_verified_before_fok BOOLEAN,
    simulated_strategy_order_registry_sha256 VARCHAR NOT NULL,
    race_complement_public_proxy_evidence_id VARCHAR,
    settlement_release_elapsed_ms DECIMAL(38, 12) NOT NULL,
    first_side VARCHAR NOT NULL,
    first_price_e4 INTEGER NOT NULL,
    first_qty_fp DECIMAL(38, 12) NOT NULL,
    first_maker_trade_fee_usd DECIMAL(38, 12) NOT NULL,
    first_maker_net_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    first_leg_basis_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    complement_reservation_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    locked_pair_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    requested_qty_fp DECIMAL(38, 12) NOT NULL,
    complement_fill_qty_fp DECIMAL(38, 12) NOT NULL,
    fok_fill_qty_fp DECIMAL(38, 12) NOT NULL,
    residual_inventory_fp DECIMAL(38, 12) NOT NULL,
    fok_book_side VARCHAR NOT NULL,
    fok_limit_price_e4 INTEGER NOT NULL,
    pre_effective_book_recv_wall_ns BIGINT,
    pre_effective_book_recv_mono_ns BIGINT,
    pre_effective_book_ingest_sequence BIGINT,
    pre_effective_book_stable_source_id VARCHAR,
    pre_effective_book_source_rows_sha256 VARCHAR,
    execution_slices_sha256 VARCHAR NOT NULL,
    gross_pnl_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_raw_total_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_aggregate_centicent_lower_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_public_l2_level_scenario_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_n_max INTEGER NOT NULL,
    taker_fee_trade_upper_before_account_rounding_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_account_rounding_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_partition_dp_no_rebate_upper_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_sql_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    taker_fee_doc_literal_tight_sensitivity_usd DECIMAL(38, 12) NOT NULL,
    fee_bound_selected_for_candidate VARCHAR NOT NULL,
    fee_accumulator_receipt_authenticated BOOLEAN NOT NULL,
    fee_schedule_provenance VARCHAR NOT NULL,
    fee_schedule_source VARCHAR NOT NULL,
    fee_partition_bound_kind VARCHAR NOT NULL,
    market_min_fill_increment_fp DECIMAL(38, 12) NOT NULL,
    trade_fee_quantum_usd DECIMAL(38, 12) NOT NULL,
    account_rounding_target_usd DECIMAL(38, 12) NOT NULL,
    target_account_class VARCHAR NOT NULL,
    account_precision_receipt_sha256 VARCHAR NOT NULL,
    doc_literal_tight_sensitivity_kind VARCHAR NOT NULL,
    forward_actual_taker_fee_usd DECIMAL(38, 12),
    forward_actual_taker_fee_provenance VARCHAR NOT NULL,
    conservative_net_pnl_usd DECIMAL(38, 12) NOT NULL,
    capital_dollar_seconds DECIMAL(38, 12) NOT NULL,
    synthetic_cancel_effective_elapsed_ms DECIMAL(38, 12) NOT NULL,
    fok_terminal_elapsed_ms DECIMAL(38, 12) NOT NULL,
    fok_candidate_reserve_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    peak_strategy_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    capital_released_at_fok_terminal BOOLEAN NOT NULL,
    market_metadata_receipt_id VARCHAR NOT NULL,
    fee_schedule_receipt_id VARCHAR NOT NULL,
    public_settlement_receipt_id VARCHAR NOT NULL,
    public_settlement_recv_wall_ns BIGINT NOT NULL,
    public_settlement_recv_mono_ns BIGINT NOT NULL,
    public_settlement_status VARCHAR NOT NULL,
    public_settlement_provenance VARCHAR NOT NULL,
    official_market_result VARCHAR NOT NULL,
    public_settlement_source_rows_sha256 VARCHAR NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    data_invalid BOOLEAN NOT NULL,
    FOREIGN KEY (postfill_action_id)
        REFERENCES postfill_v41_action(postfill_action_id),
    FOREIGN KEY (market_metadata_receipt_id)
        REFERENCES postfill_v41_market_metadata_receipt(
            market_metadata_receipt_id
        ),
    FOREIGN KEY (fee_schedule_receipt_id)
        REFERENCES postfill_v41_fee_schedule_receipt(
            fee_schedule_receipt_id
        ),
    FOREIGN KEY (public_settlement_receipt_id)
        REFERENCES postfill_v41_settlement_receipt(
            settlement_receipt_id
        ),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        action_family_version =
        'ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_1'
    ),
    CHECK (
        terminal_type IN ('CANCEL_RACE_PAIR', 'FOK_FULL', 'FOK_ZERO')
    ),
    CHECK (
        requested_qty_fp = first_qty_fp
        AND complement_fill_qty_fp + fok_fill_qty_fp
            + residual_inventory_fp = requested_qty_fp
    ),
    CHECK (first_maker_trade_fee_usd = 0),
    CHECK (
        locked_pair_capital_safe_upper_usd =
        first_leg_basis_safe_upper_usd
        + complement_reservation_safe_upper_usd
    ),
    CHECK (
        market_min_fill_increment_fp = 0.01
        AND trade_fee_quantum_usd = 0.0001
        AND account_rounding_target_usd = 0.0001
        AND target_account_class = 'DIRECT'
        AND account_precision_receipt_sha256 =
            '816528a20ee70ffc7536c02b8984a69f8730f8304656a259e2edd124e6db8da6'
    ),
    CHECK (
        fee_bound_selected_for_candidate =
        'PARTITION_DP_NO_REBATE_SAFE_UPPER'
        AND NOT fee_accumulator_receipt_authenticated
        AND taker_fee_safe_upper_usd =
            taker_fee_partition_dp_no_rebate_upper_usd
        AND taker_fee_partition_dp_no_rebate_upper_usd <=
            taker_fee_sql_safe_upper_usd
    ),
    CHECK (
        forward_actual_taker_fee_usd IS NULL
        AND forward_actual_taker_fee_provenance =
            'UNAVAILABLE_HISTORICAL_PUBLIC_L2'
    ),
    CHECK (
        public_settlement_status = 'FINALIZED'
        AND public_settlement_provenance =
            'PUBLIC_SETTLEMENT_RECEIPT_SIMULATION'
        AND official_market_result IN ('YES', 'NO')
        AND settlement_release_elapsed_ms >= fok_terminal_elapsed_ms
    ),
    CHECK (
        (terminal_type = 'CANCEL_RACE_PAIR'
         AND NOT fok_sent
         AND simulated_no_self_cross_verified_before_fok IS NULL
         AND race_complement_public_proxy_evidence_id IS NOT NULL
         AND synthetic_cancel_applied_wall_ns IS NULL
         AND synthetic_cancel_applied_mono_ns IS NULL
         AND synthetic_cancel_applied_sequence IS NULL
         AND synthetic_cancel_applied_stable_source_id IS NULL
         AND fok_processed_wall_ns IS NULL
         AND fok_processed_mono_ns IS NULL
         AND fok_processed_sequence IS NULL
         AND fok_processed_stable_source_id IS NULL
         AND pre_effective_book_recv_wall_ns IS NULL
         AND pre_effective_book_recv_mono_ns IS NULL
         AND pre_effective_book_ingest_sequence IS NULL
         AND pre_effective_book_stable_source_id IS NULL
         AND pre_effective_book_source_rows_sha256 IS NULL
         AND complement_fill_qty_fp = requested_qty_fp
         AND fok_fill_qty_fp = 0
         AND residual_inventory_fp = 0
         AND capital_released_at_fok_terminal)
        OR
        (terminal_type IN ('FOK_FULL', 'FOK_ZERO')
         AND fok_sent
         AND simulated_no_self_cross_verified_before_fok
         AND race_complement_public_proxy_evidence_id IS NULL
         AND synthetic_cancel_applied_sequence = 0
         AND fok_processed_sequence = 1
         AND synthetic_cancel_applied_wall_ns = fok_processed_wall_ns
         AND synthetic_cancel_applied_mono_ns = fok_processed_mono_ns
         AND pre_effective_book_recv_wall_ns <
             synthetic_cancel_applied_wall_ns
         AND pre_effective_book_recv_mono_ns <
             synthetic_cancel_applied_mono_ns
         AND pre_effective_book_source_rows_sha256 IS NOT NULL
         AND ((terminal_type = 'FOK_FULL'
               AND complement_fill_qty_fp = 0
               AND fok_fill_qty_fp = requested_qty_fp
               AND residual_inventory_fp = 0
               AND capital_released_at_fok_terminal)
              OR
              (terminal_type = 'FOK_ZERO'
               AND complement_fill_qty_fp = 0
               AND fok_fill_qty_fp = 0
               AND residual_inventory_fp = requested_qty_fp
               AND NOT capital_released_at_fok_terminal)))
    ),
    CHECK (
        peak_strategy_capital_safe_upper_usd =
        GREATEST(
            locked_pair_capital_safe_upper_usd,
            first_leg_basis_safe_upper_usd
            + fok_candidate_reserve_safe_upper_usd
        )
    ),
    CHECK (reconciliation_ok AND NOT data_invalid)
);

CREATE TABLE postfill_v41_fok_slice (
    fok_slice_id VARCHAR PRIMARY KEY,
    postfill_fok_outcome_id VARCHAR NOT NULL,
    contract_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL,
    slice_index INTEGER NOT NULL,
    fok_book_side VARCHAR NOT NULL,
    fok_limit_price_e4 INTEGER NOT NULL,
    source_price_e4 INTEGER NOT NULL,
    source_qty_fp DECIMAL(38, 12) NOT NULL,
    executed_qty_fp DECIMAL(38, 12) NOT NULL,
    raw_trade_fee_usd DECIMAL(38, 12) NOT NULL,
    market_min_fill_increment_fp DECIMAL(38, 12) NOT NULL,
    pre_effective_book_source_rows_sha256 VARCHAR NOT NULL,
    execution_slices_sha256 VARCHAR NOT NULL,
    FOREIGN KEY (postfill_fok_outcome_id)
        REFERENCES postfill_v41_flatten_fok_outcome(
            postfill_fok_outcome_id
        ),
    UNIQUE (postfill_fok_outcome_id, slice_index),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (
        source_price_e4 BETWEEN 10 AND 9990
        AND (
            (
                (source_price_e4 < 1000 OR source_price_e4 > 9000)
                AND source_price_e4 % 10 = 0
            )
            OR (
                source_price_e4 BETWEEN 1000 AND 9000
                AND source_price_e4 % 100 = 0
            )
        )
        AND source_price_e4 % 100 = 0
    ),
    CHECK (
        fok_limit_price_e4 BETWEEN 10 AND 9990
        AND (
            (
                (fok_limit_price_e4 < 1000 OR fok_limit_price_e4 > 9000)
                AND fok_limit_price_e4 % 10 = 0
            )
            OR (
                fok_limit_price_e4 BETWEEN 1000 AND 9000
                AND fok_limit_price_e4 % 100 = 0
            )
        )
    ),
    CHECK (
        (fok_book_side = 'ASK'
         AND source_price_e4 >= fok_limit_price_e4)
        OR
        (fok_book_side = 'BID'
         AND source_price_e4 <= fok_limit_price_e4)
    ),
    CHECK (
        slice_index >= 0
        AND source_qty_fp > 0
        AND executed_qty_fp >= 0
        AND executed_qty_fp <= source_qty_fp
        AND source_qty_fp % 0.01 = 0
        AND executed_qty_fp % 0.01 = 0
        AND market_min_fill_increment_fp = 0.01
    ),
    CHECK (
        raw_trade_fee_usd = ROUND(
        CAST(0.07 AS DECIMAL(38, 12)) * executed_qty_fp
        * (
            CAST(source_price_e4 AS DECIMAL(38, 12))
            / CAST(10000 AS DECIMAL(38, 12))
        )
        * (
            CAST(1 AS DECIMAL(38, 12))
            - CAST(source_price_e4 AS DECIMAL(38, 12))
              / CAST(10000 AS DECIMAL(38, 12))
        ),
        12
        )
    )
);

CREATE TABLE postfill_v41_public_proxy_evidence (
    evidence_id VARCHAR PRIMARY KEY,
    proxy_kind VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    maker_order_yes_book_side VARCHAR NOT NULL,
    maker_order_outcome_side VARCHAR NOT NULL,
    maker_order_outcome_price_e4 INTEGER NOT NULL,
    maker_order_yes_price_e4 INTEGER NOT NULL,
    maker_order_qty_fp DECIMAL(38, 12) NOT NULL,
    trigger_public_trade_row_id VARCHAR NOT NULL,
    derived_cumulative_strict_through_qty_fp
        DECIMAL(38, 12) NOT NULL,
    public_trade_spine_sha256 VARCHAR NOT NULL,
    recv_wall_ns BIGINT NOT NULL,
    recv_mono_ns BIGINT NOT NULL,
    ingest_sequence BIGINT NOT NULL,
    stable_source_id VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_metadata_receipt_id VARCHAR NOT NULL,
    market_metadata_source_rows_sha256 VARCHAR NOT NULL,
    FOREIGN KEY (market_metadata_receipt_id)
        REFERENCES postfill_v41_market_metadata_receipt(
            market_metadata_receipt_id
        ),
    CHECK (proxy_kind IN ('FIRST_FILL', 'COMPLEMENT_FILL')),
    CHECK (
        (
            maker_order_outcome_side = 'YES'
            AND maker_order_yes_book_side = 'BID'
            AND maker_order_yes_price_e4 =
                maker_order_outcome_price_e4
        )
        OR
        (
            maker_order_outcome_side = 'NO'
            AND maker_order_yes_book_side = 'ASK'
            AND maker_order_yes_price_e4 =
                10000 - maker_order_outcome_price_e4
        )
    ),
    CHECK (
        maker_order_qty_fp > 0
        AND derived_cumulative_strict_through_qty_fp >=
            maker_order_qty_fp
        AND maker_order_qty_fp % 0.01 = 0
        AND derived_cumulative_strict_through_qty_fp % 0.01 = 0
    )
);

CREATE TABLE postfill_v41_public_trade_row (
    public_trade_row_id VARCHAR PRIMARY KEY,
    evidence_id VARCHAR NOT NULL,
    contract_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    trade_index INTEGER NOT NULL,
    trade_id VARCHAR NOT NULL,
    taker_book_side VARCHAR NOT NULL,
    taker_outcome_side VARCHAR NOT NULL,
    yes_price_e4 INTEGER NOT NULL,
    count_fp DECIMAL(38, 12) NOT NULL,
    is_block_trade BOOLEAN NOT NULL,
    recv_wall_ns BIGINT NOT NULL,
    recv_mono_ns BIGINT NOT NULL,
    ingest_sequence BIGINT NOT NULL,
    stable_source_id VARCHAR NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    public_trade_spine_sha256 VARCHAR NOT NULL,
    FOREIGN KEY (evidence_id)
        REFERENCES postfill_v41_public_proxy_evidence(evidence_id),
    UNIQUE (evidence_id, trade_index),
    UNIQUE (market_ticker, trade_id),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (trade_index >= 0),
    CHECK (
        taker_book_side IN ('BID', 'ASK')
        AND taker_outcome_side IN ('YES', 'NO')
    ),
    CHECK (
        yes_price_e4 BETWEEN 10 AND 9990
        AND (
            (
                (yes_price_e4 < 1000 OR yes_price_e4 > 9000)
                AND yes_price_e4 % 10 = 0
            )
            OR (
                yes_price_e4 BETWEEN 1000 AND 9000
                AND yes_price_e4 % 100 = 0
            )
        )
    ),
    CHECK (
        count_fp > 0
        AND count_fp % 0.01 = 0
        AND NOT is_block_trade
    )
);

CREATE TABLE postfill_v41_zero_time_atom (
    postfill_episode_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    market_metadata_receipt_id VARCHAR NOT NULL,
    settlement_receipt_id VARCHAR NOT NULL,
    fee_schedule_receipt_id VARCHAR NOT NULL,
    data_origin VARCHAR NOT NULL,
    first_fill_provenance VARCHAR NOT NULL,
    complement_fill_provenance VARCHAR NOT NULL,
    first_fill_execution_nature VARCHAR NOT NULL,
    complement_execution_nature VARCHAR NOT NULL,
    first_fill_trade_fee_provenance VARCHAR NOT NULL,
    complement_trade_fee_provenance VARCHAR NOT NULL,
    first_fill_public_proxy_evidence_id VARCHAR NOT NULL,
    complement_public_proxy_evidence_id VARCHAR NOT NULL,
    receipt_envelope_id VARCHAR NOT NULL,
    atom_recv_wall_ns BIGINT NOT NULL,
    atom_recv_mono_ns BIGINT NOT NULL,
    first_fill_ingest_sequence BIGINT NOT NULL,
    complement_fill_ingest_sequence BIGINT NOT NULL,
    first_side VARCHAR NOT NULL,
    complement_side VARCHAR NOT NULL,
    first_price_e4 INTEGER NOT NULL,
    complement_price_e4 INTEGER NOT NULL,
    first_qty_fp DECIMAL(38, 12) NOT NULL,
    complement_qty_fp DECIMAL(38, 12) NOT NULL,
    first_leg_basis_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    complement_reservation_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    locked_pair_capital_safe_upper_usd DECIMAL(38, 12) NOT NULL,
    source_rows_sha256 VARCHAR NOT NULL,
    reconciliation_ok BOOLEAN NOT NULL,
    FOREIGN KEY (first_fill_public_proxy_evidence_id)
        REFERENCES postfill_v41_public_proxy_evidence(evidence_id),
    FOREIGN KEY (complement_public_proxy_evidence_id)
        REFERENCES postfill_v41_public_proxy_evidence(evidence_id),
    FOREIGN KEY (market_metadata_receipt_id)
        REFERENCES postfill_v41_market_metadata_receipt(
            market_metadata_receipt_id
        ),
    FOREIGN KEY (settlement_receipt_id)
        REFERENCES postfill_v41_settlement_receipt(
            settlement_receipt_id
        ),
    FOREIGN KEY (fee_schedule_receipt_id)
        REFERENCES postfill_v41_fee_schedule_receipt(
            fee_schedule_receipt_id
        ),
    CHECK (contract_version = 'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'),
    CHECK (data_origin = 'PUBLIC_RAW'),
    CHECK (
        first_fill_provenance =
            'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
        AND complement_fill_provenance =
            'PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY'
        AND first_fill_execution_nature = 'SYNTHETIC'
        AND complement_execution_nature = 'SYNTHETIC'
        AND first_fill_trade_fee_provenance =
            'SCHEDULE_ZERO_MAKER_TRADE_FEE'
        AND complement_trade_fee_provenance =
            'SCHEDULE_ZERO_MAKER_TRADE_FEE'
    ),
    CHECK (
        complement_fill_ingest_sequence > first_fill_ingest_sequence
        AND complement_side <> first_side
        AND complement_qty_fp = first_qty_fp
    ),
    CHECK (
        locked_pair_capital_safe_upper_usd =
        first_leg_basis_safe_upper_usd
        + complement_reservation_safe_upper_usd
    ),
    CHECK (reconciliation_ok)
);
