-- Normative ROUND4 post-fill V4.1 batch validator.
--
-- The table-builder executes this file after all eleven normalized tables
-- have been inserted inside one transaction.  Any returned row is fatal and
-- rolls the transaction back.  This SQL independently walks FOK source
-- levels and recomputes quantity, gross PnL, fee bounds, cash, and capital;
-- it does not trust caller-supplied aggregate economics.

CREATE OR REPLACE TEMP VIEW postfill_v41_validation_violations AS
WITH
action_pair AS (
    SELECT
        postfill_decision_id,
        COUNT(*) AS action_count,
        SUM(CASE WHEN action_kind = 'KEEP' THEN 1 ELSE 0 END) AS keep_count,
        SUM(
            CASE WHEN action_kind = 'FLATTEN_FOK' THEN 1 ELSE 0 END
        ) AS fok_count
    FROM postfill_v41_action
    GROUP BY postfill_decision_id
),
slice_walk AS (
    SELECT
        s.*,
        o.terminal_type,
        o.requested_qty_fp,
        o.first_side,
        o.first_price_e4,
        COALESCE(
            SUM(s.source_qty_fp) OVER (
                PARTITION BY s.postfill_fok_outcome_id
                ORDER BY s.slice_index
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS prior_source_qty,
        LAG(s.source_price_e4) OVER (
            PARTITION BY s.postfill_fok_outcome_id
            ORDER BY s.slice_index
        ) AS prior_price_e4
    FROM postfill_v41_fok_slice AS s
    JOIN postfill_v41_flatten_fok_outcome AS o
      ON o.postfill_fok_outcome_id = s.postfill_fok_outcome_id
),
slice_math AS (
    SELECT
        *,
        CASE
            WHEN terminal_type = 'FOK_FULL'
            THEN LEAST(
                source_qty_fp,
                GREATEST(requested_qty_fp - prior_source_qty, 0)
            )
            ELSE 0
        END AS expected_executed_qty_fp,
        ROUND(
            CAST(0.07 AS DECIMAL(38, 12))
            * executed_qty_fp
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
        ) AS recomputed_raw_fee_usd,
        CASE
            WHEN first_side = 'YES'
            THEN (
                CAST(source_price_e4 - first_price_e4 AS DECIMAL(38, 12))
                / CAST(10000 AS DECIMAL(38, 12))
            ) * executed_qty_fp
            ELSE (
                CAST(
                    10000 - source_price_e4 - first_price_e4
                    AS DECIMAL(38, 12)
                )
                / CAST(10000 AS DECIMAL(38, 12))
            ) * executed_qty_fp
        END AS recomputed_gross_usd,
        CEIL(
            (
                CAST(0.07 AS DECIMAL(38, 12))
                * CAST(0.01 AS DECIMAL(38, 12))
                * (
                    CAST(source_price_e4 AS DECIMAL(38, 12))
                    / CAST(10000 AS DECIMAL(38, 12))
                )
                * (
                    CAST(1 AS DECIMAL(38, 12))
                    - CAST(source_price_e4 AS DECIMAL(38, 12))
                      / CAST(10000 AS DECIMAL(38, 12))
                )
            ) * 10000
        ) / 10000 AS min_fill_trade_fee_usd
    FROM slice_walk
),
slice_aggregate AS (
    SELECT
        postfill_fok_outcome_id,
        COUNT(*) AS slice_count,
        COUNT(DISTINCT slice_index) AS distinct_slice_count,
        MIN(slice_index) AS min_slice_index,
        MAX(slice_index) AS max_slice_index,
        SUM(source_qty_fp) AS source_qty_fp,
        SUM(executed_qty_fp) AS executed_qty_fp,
        SUM(expected_executed_qty_fp) AS expected_executed_qty_fp,
        SUM(recomputed_raw_fee_usd) AS raw_total_usd,
        CEIL(SUM(recomputed_raw_fee_usd) * 10000) / 10000
            AS aggregate_centicent_lower_usd,
        SUM(CEIL(recomputed_raw_fee_usd * 10000) / 10000)
            AS public_l2_level_scenario_usd,
        CAST(SUM(executed_qty_fp / 0.01) AS BIGINT) AS n_max,
        SUM(
            (executed_qty_fp / 0.01) * min_fill_trade_fee_usd
        ) AS trade_upper_before_account_rounding_usd,
        SUM(recomputed_gross_usd) AS gross_usd,
        SUM(
            CASE
                WHEN executed_qty_fp <> expected_executed_qty_fp THEN 1
                ELSE 0
            END
        ) AS execution_walk_errors,
        SUM(
            CASE
                WHEN ABS(raw_trade_fee_usd - recomputed_raw_fee_usd)
                     > 0.000000000001
                THEN 1 ELSE 0
            END
        ) AS raw_fee_errors,
        SUM(
            CASE
                WHEN prior_price_e4 IS NULL THEN 0
                WHEN fok_book_side = 'ASK'
                     AND source_price_e4 >= prior_price_e4
                THEN 1
                WHEN fok_book_side = 'BID'
                     AND source_price_e4 <= prior_price_e4
                THEN 1
                ELSE 0
            END
        ) AS price_order_errors,
        COUNT(DISTINCT pre_effective_book_source_rows_sha256)
            AS source_hash_count,
        COUNT(DISTINCT execution_slices_sha256)
            AS execution_hash_count
    FROM slice_math
    GROUP BY postfill_fok_outcome_id
),
outcome_with_state AS (
    SELECT
        o.*,
        s.decision_recv_wall_ns,
        s.decision_recv_mono_ns,
        s.decision_elapsed_ms,
        s.complement_price_e4,
        s.fok_required_principal_usd AS state_fok_principal_usd,
        s.fok_expected_sale_proceeds_usd AS state_sale_proceeds_usd,
        s.fok_required_fee_safe_upper_usd AS state_fee_upper_usd,
        s.fok_required_cash_with_fee_safe_upper_usd
            AS state_cash_required_usd,
        s.simulated_available_cash_after_cancel_usd,
        s.fok_candidate_reserve_safe_upper_usd
            AS state_candidate_reserve_usd,
        s.peak_strategy_capital_safe_upper_usd
            AS state_peak_capital_usd,
        COALESCE(a.slice_count, 0) AS slice_count,
        COALESCE(a.distinct_slice_count, 0) AS distinct_slice_count,
        COALESCE(a.min_slice_index, 0) AS min_slice_index,
        COALESCE(a.max_slice_index, -1) AS max_slice_index,
        COALESCE(a.source_qty_fp, 0) AS source_qty_fp,
        COALESCE(a.executed_qty_fp, 0) AS slice_executed_qty_fp,
        COALESCE(a.expected_executed_qty_fp, 0)
            AS expected_executed_qty_fp,
        COALESCE(a.raw_total_usd, 0) AS recomputed_raw_total_usd,
        COALESCE(a.aggregate_centicent_lower_usd, 0)
            AS recomputed_aggregate_lower_usd,
        COALESCE(a.public_l2_level_scenario_usd, 0)
            AS recomputed_l2_scenario_usd,
        COALESCE(a.n_max, 0) AS recomputed_n_max,
        COALESCE(a.trade_upper_before_account_rounding_usd, 0)
            AS recomputed_trade_upper_usd,
        COALESCE(a.gross_usd, 0) AS recomputed_slice_gross_usd,
        COALESCE(a.execution_walk_errors, 0) AS execution_walk_errors,
        COALESCE(a.raw_fee_errors, 0) AS raw_fee_errors,
        COALESCE(a.price_order_errors, 0) AS price_order_errors,
        COALESCE(a.source_hash_count, 0) AS source_hash_count,
        COALESCE(a.execution_hash_count, 0) AS execution_hash_count,
        (
            s.decision_recv_wall_ns
            - CAST(s.decision_elapsed_ms * 1000000 AS BIGINT)
        ) AS first_recv_wall_ns,
        (
            s.decision_recv_mono_ns
            - CAST(s.decision_elapsed_ms * 1000000 AS BIGINT)
        ) AS first_recv_mono_ns
    FROM postfill_v41_flatten_fok_outcome AS o
    JOIN postfill_v41_causal_state AS s
      ON s.postfill_episode_id = o.postfill_episode_id
     AND s.decision_index = o.decision_index
    LEFT JOIN slice_aggregate AS a
      ON a.postfill_fok_outcome_id = o.postfill_fok_outcome_id
),
outcome_math AS (
    SELECT
        *,
        recomputed_trade_upper_usd
            + recomputed_n_max * account_rounding_target_usd
            AS recomputed_sql_safe_upper_usd,
        -- Candidate rows admit whole-cent FOK levels on a DIRECT h=tau
        -- account.  Therefore every legal private partition has zero
        -- account remainder and the exact partition-DP upper equals Tmax.
        recomputed_trade_upper_usd
            AS recomputed_partition_dp_upper_usd,
        recomputed_raw_total_usd
            + recomputed_n_max * trade_fee_quantum_usd
            + CASE WHEN recomputed_n_max > 0 THEN 0.01 ELSE 0 END
            AS recomputed_doc_literal_sensitivity_usd,
        CASE
            WHEN terminal_type = 'FOK_FULL'
            THEN recomputed_slice_gross_usd
            WHEN terminal_type = 'FOK_ZERO'
            THEN (
                CASE
                    WHEN official_market_result = first_side
                    THEN (
                        CAST(10000 - first_price_e4 AS DECIMAL(38, 12))
                        / CAST(10000 AS DECIMAL(38, 12))
                    )
                    ELSE (
                        -CAST(first_price_e4 AS DECIMAL(38, 12))
                        / CAST(10000 AS DECIMAL(38, 12))
                    )
                END
            ) * first_qty_fp
            ELSE (
                (
                    CAST(10000 - first_price_e4 AS DECIMAL(38, 12))
                    / CAST(10000 AS DECIMAL(38, 12))
                ) * first_qty_fp
                - complement_reservation_safe_upper_usd
            )
        END AS recomputed_gross_usd,
        CASE
            WHEN terminal_type = 'CANCEL_RACE_PAIR'
            THEN (
                locked_pair_capital_safe_upper_usd
                * terminal_elapsed_ms
            ) / 1000
            WHEN terminal_type = 'FOK_FULL'
            THEN (
                locked_pair_capital_safe_upper_usd
                * synthetic_cancel_effective_elapsed_ms
                + (
                    first_leg_basis_safe_upper_usd
                    + fok_candidate_reserve_safe_upper_usd
                )
                * (
                    fok_terminal_elapsed_ms
                    - synthetic_cancel_effective_elapsed_ms
                )
            ) / 1000
            ELSE (
                locked_pair_capital_safe_upper_usd
                * synthetic_cancel_effective_elapsed_ms
                + (
                    first_leg_basis_safe_upper_usd
                    + fok_candidate_reserve_safe_upper_usd
                )
                * (
                    fok_terminal_elapsed_ms
                    - synthetic_cancel_effective_elapsed_ms
                )
                + first_leg_basis_safe_upper_usd
                * (
                    settlement_release_elapsed_ms
                    - fok_terminal_elapsed_ms
                )
            ) / 1000
        END AS recomputed_capital_dollar_seconds
    FROM outcome_with_state
),
first_proxy_count AS (
    SELECT
        postfill_episode_id,
        COUNT(*) AS first_proxy_count
    FROM postfill_v41_public_proxy_evidence
    WHERE proxy_kind = 'FIRST_FILL'
    GROUP BY postfill_episode_id
),
public_trade_walk AS (
    SELECT
        t.*,
        e.proxy_kind,
        e.market_ticker AS evidence_market_ticker,
        e.market_id AS evidence_market_id,
        e.postfill_episode_id AS evidence_episode_id,
        e.source_date_utc AS evidence_source_date_utc,
        e.maker_order_yes_book_side,
        e.maker_order_outcome_side,
        e.maker_order_outcome_price_e4,
        e.maker_order_yes_price_e4,
        e.maker_order_qty_fp,
        e.trigger_public_trade_row_id,
        e.derived_cumulative_strict_through_qty_fp,
        e.public_trade_spine_sha256 AS evidence_spine_sha256,
        e.recv_wall_ns AS evidence_recv_wall_ns,
        e.recv_mono_ns AS evidence_recv_mono_ns,
        e.ingest_sequence AS evidence_ingest_sequence,
        e.stable_source_id AS evidence_stable_source_id,
        LAG(t.recv_wall_ns) OVER (
            PARTITION BY t.evidence_id
            ORDER BY t.trade_index
        ) AS prior_recv_wall_ns,
        LAG(t.recv_mono_ns) OVER (
            PARTITION BY t.evidence_id
            ORDER BY t.trade_index
        ) AS prior_recv_mono_ns,
        LAG(t.ingest_sequence) OVER (
            PARTITION BY t.evidence_id
            ORDER BY t.trade_index
        ) AS prior_ingest_sequence,
        LAG(t.stable_source_id) OVER (
            PARTITION BY t.evidence_id
            ORDER BY t.trade_index
        ) AS prior_stable_source_id
    FROM postfill_v41_public_trade_row AS t
    JOIN postfill_v41_public_proxy_evidence AS e
      ON e.evidence_id = t.evidence_id
),
public_trade_aggregate AS (
    SELECT
        evidence_id,
        COUNT(*) AS trade_count,
        COUNT(DISTINCT trade_index) AS distinct_trade_index_count,
        MIN(trade_index) AS min_trade_index,
        MAX(trade_index) AS max_trade_index,
        SUM(count_fp) AS cumulative_qty_fp,
        COUNT(DISTINCT public_trade_spine_sha256)
            AS row_spine_hash_count,
        ARG_MAX(public_trade_row_id, trade_index)
            AS final_public_trade_row_id,
        ARG_MAX(recv_wall_ns, trade_index) AS final_recv_wall_ns,
        ARG_MAX(recv_mono_ns, trade_index) AS final_recv_mono_ns,
        ARG_MAX(ingest_sequence, trade_index)
            AS final_ingest_sequence,
        ARG_MAX(stable_source_id, trade_index)
            AS final_stable_source_id,
        SUM(
            CASE
                WHEN contract_version <>
                     'ROUND4_POSTFILL_PUBLIC_PROXY_V4_1'
                  OR source_date_utc <> evidence_source_date_utc
                  OR market_ticker <> evidence_market_ticker
                  OR market_id <> evidence_market_id
                  OR postfill_episode_id <> evidence_episode_id
                  OR public_trade_row_id <>
                     evidence_id || '::TRADE::'
                     || CAST(trade_index AS VARCHAR)
                THEN 1 ELSE 0
            END
        ) AS identity_errors,
        SUM(
            CASE
                WHEN public_trade_spine_sha256 <>
                     evidence_spine_sha256
                THEN 1 ELSE 0
            END
        ) AS spine_hash_errors,
        SUM(
            CASE
                WHEN (
                    maker_order_outcome_side = 'YES'
                    AND (
                        maker_order_yes_book_side <> 'BID'
                        OR taker_book_side <> 'ASK'
                        OR taker_outcome_side <> 'NO'
                        OR yes_price_e4 >= maker_order_yes_price_e4
                    )
                )
                OR (
                    maker_order_outcome_side = 'NO'
                    AND (
                        maker_order_yes_book_side <> 'ASK'
                        OR taker_book_side <> 'BID'
                        OR taker_outcome_side <> 'YES'
                        OR yes_price_e4 <= maker_order_yes_price_e4
                    )
                )
                THEN 1 ELSE 0
            END
        ) AS taker_semantic_errors,
        SUM(
            CASE
                WHEN prior_recv_wall_ns IS NULL THEN 0
                WHEN recv_wall_ns < prior_recv_wall_ns
                  OR (
                    recv_wall_ns = prior_recv_wall_ns
                    AND recv_mono_ns < prior_recv_mono_ns
                  )
                  OR (
                    recv_wall_ns = prior_recv_wall_ns
                    AND recv_mono_ns = prior_recv_mono_ns
                    AND ingest_sequence < prior_ingest_sequence
                  )
                  OR (
                    recv_wall_ns = prior_recv_wall_ns
                    AND recv_mono_ns = prior_recv_mono_ns
                    AND ingest_sequence = prior_ingest_sequence
                    AND stable_source_id <= prior_stable_source_id
                  )
                THEN 1 ELSE 0
            END
        ) AS receipt_order_errors
    FROM public_trade_walk
    GROUP BY evidence_id
),
settlement_projection AS (
    SELECT
        'KEEP' AS projection_kind,
        postfill_keep_transition_id AS row_key,
        source_date_utc,
        market_ticker,
        market_id,
        public_settlement_receipt_id,
        public_settlement_recv_wall_ns,
        public_settlement_recv_mono_ns,
        public_settlement_status,
        official_market_result,
        public_settlement_source_rows_sha256
    FROM postfill_v41_keep_transition
    UNION ALL
    SELECT
        'FOK_OUTCOME',
        postfill_fok_outcome_id,
        source_date_utc,
        market_ticker,
        market_id,
        public_settlement_receipt_id,
        public_settlement_recv_wall_ns,
        public_settlement_recv_mono_ns,
        public_settlement_status,
        official_market_result,
        public_settlement_source_rows_sha256
    FROM postfill_v41_flatten_fok_outcome
),
fee_projection AS (
    SELECT
        'STATE' AS projection_kind,
        postfill_decision_id AS row_key,
        source_date_utc,
        market_ticker,
        fee_schedule_receipt_id,
        fee_schedule_source_rows_sha256
    FROM postfill_v41_causal_state
    UNION ALL
    SELECT
        'KEEP',
        postfill_keep_transition_id,
        source_date_utc,
        market_ticker,
        fee_schedule_receipt_id,
        CAST(NULL AS VARCHAR)
    FROM postfill_v41_keep_transition
    UNION ALL
    SELECT
        'FOK_OUTCOME',
        postfill_fok_outcome_id,
        source_date_utc,
        market_ticker,
        fee_schedule_receipt_id,
        CAST(NULL AS VARCHAR)
    FROM postfill_v41_flatten_fok_outcome
    UNION ALL
    SELECT
        'ZERO_ATOM',
        postfill_episode_id,
        source_date_utc,
        market_ticker,
        fee_schedule_receipt_id,
        CAST(NULL AS VARCHAR)
    FROM postfill_v41_zero_time_atom
)
SELECT
    'ACTION_PAIR_CARDINALITY' AS violation_code,
    s.postfill_decision_id AS row_key,
    'each state requires exactly KEEP+FLATTEN_FOK' AS detail
FROM postfill_v41_causal_state AS s
LEFT JOIN action_pair AS p USING (postfill_decision_id)
WHERE COALESCE(p.action_count, 0) <> 2
   OR COALESCE(p.keep_count, 0) <> 1
   OR COALESCE(p.fok_count, 0) <> 1

UNION ALL

SELECT
    'ORPHAN_ACTION',
    a.postfill_action_id,
    'action has no causal-state parent'
FROM postfill_v41_action AS a
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_decision_id = a.postfill_decision_id
WHERE s.postfill_decision_id IS NULL

UNION ALL

SELECT
    'ACTION_STATE_DRIFT',
    a.postfill_action_id,
    'action linkage/economics differ from causal state'
FROM postfill_v41_action AS a
JOIN postfill_v41_causal_state AS s USING (postfill_decision_id)
WHERE a.source_date_utc <> s.source_date_utc
   OR a.market_ticker <> s.market_ticker
   OR a.market_id <> s.market_id
   OR a.postfill_episode_id <> s.postfill_episode_id
   OR a.decision_index <> s.decision_index
   OR a.paired_state_fingerprint_sha256 <>
        s.paired_state_fingerprint_sha256
   OR a.requested_qty_fp <> s.first_qty_fp
   OR a.complement_reservation_safe_upper_usd <>
        s.complement_reservation_safe_upper_usd
   OR a.locked_pair_capital_safe_upper_usd <>
        s.locked_pair_capital_safe_upper_usd
   OR a.simulated_available_cash_after_cancel_usd <>
        s.simulated_available_cash_after_cancel_usd
   OR (
        a.action_kind = 'FLATTEN_FOK'
        AND (
            a.fok_book_side <> s.flatten_book_side
            OR a.fok_limit_price_e4 <> s.flatten_limit_price_e4
            OR a.fok_required_principal_usd <>
                s.fok_required_principal_usd
            OR a.fok_expected_sale_proceeds_usd <>
                s.fok_expected_sale_proceeds_usd
            OR a.fok_required_fee_safe_upper_usd <>
                s.fok_required_fee_safe_upper_usd
            OR a.fok_required_cash_with_fee_safe_upper_usd <>
                s.fok_required_cash_with_fee_safe_upper_usd
            OR a.fok_candidate_reserve_safe_upper_usd <>
                s.fok_candidate_reserve_safe_upper_usd
            OR a.peak_strategy_capital_safe_upper_usd <>
                s.peak_strategy_capital_safe_upper_usd
        )
   )

UNION ALL

SELECT
    'KEEP_TRANSITION_COVERAGE',
    a.postfill_action_id,
    'KEEP action requires exactly one transition'
FROM postfill_v41_action AS a
LEFT JOIN postfill_v41_keep_transition AS k
  ON k.keep_action_id = a.postfill_action_id
WHERE a.action_kind = 'KEEP'
GROUP BY a.postfill_action_id
HAVING COUNT(k.postfill_keep_transition_id) <> 1

UNION ALL

SELECT
    'ORPHAN_KEEP_TRANSITION',
    k.postfill_keep_transition_id,
    'KEEP transition parent action/state/next state is missing or wrong'
FROM postfill_v41_keep_transition AS k
LEFT JOIN postfill_v41_action AS a
  ON a.postfill_action_id = k.keep_action_id
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_decision_id = k.from_decision_id
LEFT JOIN postfill_v41_causal_state AS n
  ON n.postfill_decision_id = k.next_decision_id
WHERE a.postfill_action_id IS NULL
   OR a.action_kind <> 'KEEP'
   OR s.postfill_decision_id IS NULL
   OR a.postfill_decision_id <> k.from_decision_id
   OR (
        k.next_decision_id IS NOT NULL
        AND n.postfill_decision_id IS NULL
      )

UNION ALL

SELECT
    'MARKET_IDENTITY_DATE',
    postfill_decision_id,
    'ticker expiry token does not match source_date_utc'
FROM postfill_v41_causal_state
WHERE SUBSTR(market_ticker, 10, 7) <>
      (
          SUBSTR(source_date_utc, 3, 2)
          || CASE SUBSTR(source_date_utc, 6, 2)
                WHEN '01' THEN 'JAN'
                WHEN '02' THEN 'FEB'
                WHEN '03' THEN 'MAR'
                WHEN '04' THEN 'APR'
                WHEN '05' THEN 'MAY'
                WHEN '06' THEN 'JUN'
                WHEN '07' THEN 'JUL'
                WHEN '08' THEN 'AUG'
                WHEN '09' THEN 'SEP'
                WHEN '10' THEN 'OCT'
                WHEN '11' THEN 'NOV'
                WHEN '12' THEN 'DEC'
             END
          || SUBSTR(source_date_utc, 9, 2)
      )

UNION ALL

SELECT
    'MARKET_METADATA_STATE_LINK',
    s.postfill_decision_id,
    'state identity/window/hash is not derived from metadata receipt'
FROM postfill_v41_causal_state AS s
LEFT JOIN postfill_v41_market_metadata_receipt AS m
  ON m.market_metadata_receipt_id = s.market_metadata_receipt_id
WHERE m.market_metadata_receipt_id IS NULL
   OR m.source_date_utc <> s.source_date_utc
   OR m.market_ticker <> s.market_ticker
   OR m.market_id <> s.market_id
   OR m.source_rows_sha256 <>
        s.market_metadata_source_rows_sha256
   OR m.market_min_fill_increment_fp <>
        s.market_min_fill_increment_fp
   OR (
        s.decision_recv_wall_ns
        - CAST(s.decision_elapsed_ms * 1000000 AS BIGINT)
      ) < m.market_window_open_wall_ns
   OR (
        s.decision_recv_wall_ns
        - CAST(s.decision_elapsed_ms * 1000000 AS BIGINT)
      ) >= m.market_window_close_wall_ns
   OR ABS(
        s.market_close_elapsed_ms
        - (
            CAST(
                m.market_window_close_wall_ns
                - (
                    s.decision_recv_wall_ns
                    - CAST(
                        s.decision_elapsed_ms * 1000000
                        AS BIGINT
                    )
                )
                AS DECIMAL(38, 12)
            ) / 1000000
          )
      ) > 0.000000000001

UNION ALL

SELECT
    'MARKET_METADATA_PROXY_LINK',
    e.evidence_id,
    'proxy identity/window/hash is not bound to metadata receipt'
FROM postfill_v41_public_proxy_evidence AS e
LEFT JOIN postfill_v41_market_metadata_receipt AS m
  ON m.market_metadata_receipt_id = e.market_metadata_receipt_id
WHERE m.market_metadata_receipt_id IS NULL
   OR m.source_date_utc <> e.source_date_utc
   OR m.market_ticker <> e.market_ticker
   OR m.market_id <> e.market_id
   OR m.source_rows_sha256 <>
        e.market_metadata_source_rows_sha256
   OR e.recv_wall_ns < m.market_window_open_wall_ns
   OR e.recv_wall_ns >= m.market_window_close_wall_ns

UNION ALL

SELECT
    'MARKET_METADATA_KEEP_LINK',
    k.postfill_keep_transition_id,
    'KEEP metadata receipt differs from its causal-state receipt'
FROM postfill_v41_keep_transition AS k
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_decision_id = k.from_decision_id
LEFT JOIN postfill_v41_market_metadata_receipt AS m
  ON m.market_metadata_receipt_id = k.market_metadata_receipt_id
WHERE s.postfill_decision_id IS NULL
   OR m.market_metadata_receipt_id IS NULL
   OR k.market_metadata_receipt_id <>
        s.market_metadata_receipt_id
   OR m.source_date_utc <> k.source_date_utc
   OR m.market_ticker <> k.market_ticker
   OR m.market_id <> k.market_id

UNION ALL

SELECT
    'MARKET_METADATA_OUTCOME_LINK',
    o.postfill_fok_outcome_id,
    'FOK outcome metadata differs from its causal-state receipt'
FROM postfill_v41_flatten_fok_outcome AS o
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_episode_id = o.postfill_episode_id
 AND s.decision_index = o.decision_index
LEFT JOIN postfill_v41_market_metadata_receipt AS m
  ON m.market_metadata_receipt_id = o.market_metadata_receipt_id
WHERE s.postfill_decision_id IS NULL
   OR m.market_metadata_receipt_id IS NULL
   OR o.market_metadata_receipt_id <>
        s.market_metadata_receipt_id
   OR m.source_date_utc <> o.source_date_utc
   OR m.market_ticker <> o.market_ticker
   OR m.market_id <> o.market_id

UNION ALL

SELECT
    'MARKET_METADATA_ZERO_LINK',
    z.postfill_episode_id,
    'zero atom identity/window is not bound to metadata receipt'
FROM postfill_v41_zero_time_atom AS z
LEFT JOIN postfill_v41_market_metadata_receipt AS m
  ON m.market_metadata_receipt_id = z.market_metadata_receipt_id
WHERE m.market_metadata_receipt_id IS NULL
   OR m.source_date_utc <> z.source_date_utc
   OR m.market_ticker <> z.market_ticker
   OR m.market_id <> z.market_id
   OR z.atom_recv_wall_ns < m.market_window_open_wall_ns
   OR z.atom_recv_wall_ns >= m.market_window_close_wall_ns

UNION ALL

SELECT
    'FEE_SCHEDULE_RECEIPT_LINK',
    p.row_key,
    'projection does not use the frozen effective series fee receipt'
FROM fee_projection AS p
LEFT JOIN postfill_v41_fee_schedule_receipt AS f
  ON f.fee_schedule_receipt_id = p.fee_schedule_receipt_id
WHERE f.fee_schedule_receipt_id IS NULL
   OR p.source_date_utc < f.schedule_effective_date_utc
   OR p.market_ticker NOT LIKE f.series_ticker || '-%'
   OR (
        p.fee_schedule_source_rows_sha256 IS NOT NULL
        AND p.fee_schedule_source_rows_sha256 <>
            f.source_rows_sha256
      )

UNION ALL

SELECT
    'OUTCOME_COVERAGE',
    a.postfill_action_id,
    'FLATTEN_FOK action requires exactly one outcome'
FROM postfill_v41_action AS a
LEFT JOIN postfill_v41_flatten_fok_outcome AS o
  ON o.postfill_action_id = a.postfill_action_id
WHERE a.action_kind = 'FLATTEN_FOK'
GROUP BY a.postfill_action_id
HAVING COUNT(o.postfill_fok_outcome_id) <> 1

UNION ALL

SELECT
    'ORPHAN_OUTCOME',
    o.postfill_fok_outcome_id,
    'FOK outcome has no matching FLATTEN_FOK action/state parent'
FROM postfill_v41_flatten_fok_outcome AS o
LEFT JOIN postfill_v41_action AS a
  ON a.postfill_action_id = o.postfill_action_id
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_decision_id = a.postfill_decision_id
WHERE a.postfill_action_id IS NULL
   OR a.action_kind <> 'FLATTEN_FOK'
   OR s.postfill_decision_id IS NULL
   OR o.source_date_utc <> a.source_date_utc
   OR o.market_ticker <> a.market_ticker
   OR o.market_id <> a.market_id
   OR o.postfill_episode_id <> a.postfill_episode_id
   OR o.decision_index <> a.decision_index
   OR o.fok_book_side <> a.fok_book_side
   OR o.fok_limit_price_e4 <> a.fok_limit_price_e4

UNION ALL

SELECT
    'ORPHAN_SLICE',
    s.fok_slice_id,
    'FOK slice has no outcome parent'
FROM postfill_v41_fok_slice AS s
LEFT JOIN postfill_v41_flatten_fok_outcome AS o
  ON o.postfill_fok_outcome_id = s.postfill_fok_outcome_id
WHERE o.postfill_fok_outcome_id IS NULL

UNION ALL

SELECT
    'SLICE_PARENT_DRIFT',
    s.fok_slice_id,
    'FOK slice identity/book/hash fields differ from outcome parent'
FROM postfill_v41_fok_slice AS s
JOIN postfill_v41_flatten_fok_outcome AS o
  ON o.postfill_fok_outcome_id = s.postfill_fok_outcome_id
WHERE s.contract_version <> o.contract_version
   OR s.source_date_utc <> o.source_date_utc
   OR s.market_ticker <> o.market_ticker
   OR s.market_id <> o.market_id
   OR s.postfill_episode_id <> o.postfill_episode_id
   OR s.decision_index <> o.decision_index
   OR s.fok_book_side <> o.fok_book_side
   OR s.fok_limit_price_e4 <> o.fok_limit_price_e4
   OR s.pre_effective_book_source_rows_sha256 <>
        o.pre_effective_book_source_rows_sha256
   OR s.execution_slices_sha256 <> o.execution_slices_sha256

UNION ALL

SELECT
    'SLICE_CONTIGUITY',
    postfill_fok_outcome_id,
    'slice sequence/hash/source spine is not normalized'
FROM outcome_math
WHERE terminal_type IN ('FOK_FULL', 'FOK_ZERO')
  AND (
      slice_count = 0
      OR distinct_slice_count <> slice_count
      OR min_slice_index <> 0
      OR max_slice_index <> slice_count - 1
      OR source_hash_count <> 1
      OR execution_hash_count <> 1
      OR execution_walk_errors <> 0
      OR raw_fee_errors <> 0
      OR price_order_errors <> 0
  )

UNION ALL

SELECT
    'SLICE_TERMINAL_CONTRADICTION',
    postfill_fok_outcome_id,
    'canonical public-book walk contradicts FULL/ZERO/race terminal'
FROM outcome_math
WHERE (
        terminal_type = 'FOK_FULL'
        AND (
            source_qty_fp < requested_qty_fp
            OR slice_executed_qty_fp <> requested_qty_fp
            OR expected_executed_qty_fp <> requested_qty_fp
        )
      )
   OR (
        terminal_type = 'FOK_ZERO'
        AND (
            source_qty_fp >= requested_qty_fp
            OR slice_executed_qty_fp <> 0
            OR expected_executed_qty_fp <> 0
        )
      )
   OR (
        terminal_type = 'CANCEL_RACE_PAIR'
        AND slice_count <> 0
      )

UNION ALL

SELECT
    'FOK_AGGREGATE_ECONOMICS',
    postfill_fok_outcome_id,
    'gross/fee/candidate net differs from independent slice recomputation'
FROM outcome_math
WHERE ABS(gross_pnl_usd - recomputed_gross_usd) > 0.000000000001
   OR ABS(
        taker_fee_raw_total_usd - recomputed_raw_total_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_aggregate_centicent_lower_usd
        - recomputed_aggregate_lower_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_public_l2_level_scenario_usd
        - recomputed_l2_scenario_usd
      ) > 0.000000000001
   OR taker_fee_n_max <> recomputed_n_max
   OR ABS(
        taker_fee_trade_upper_before_account_rounding_usd
        - recomputed_trade_upper_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_account_rounding_safe_upper_usd
        - recomputed_n_max * account_rounding_target_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_partition_dp_no_rebate_upper_usd
        - recomputed_partition_dp_upper_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_sql_safe_upper_usd
        - recomputed_sql_safe_upper_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_safe_upper_usd
        - recomputed_partition_dp_upper_usd
      ) > 0.000000000001
   OR ABS(
        taker_fee_doc_literal_tight_sensitivity_usd
        - recomputed_doc_literal_sensitivity_usd
      ) > 0.000000000001
   OR ABS(
        conservative_net_pnl_usd
        - (
            recomputed_gross_usd
            - first_maker_net_fee_safe_upper_usd
            - CASE
                WHEN terminal_type = 'FOK_FULL'
                THEN recomputed_partition_dp_upper_usd
                ELSE 0
              END
          )
      ) > 0.000000000001

UNION ALL

SELECT
    'FOK_FEE_SCHEDULE_PIN',
    postfill_fok_outcome_id,
    'outcome fee source/provenance differs from the frozen contract'
FROM postfill_v41_flatten_fok_outcome
WHERE fee_schedule_provenance <> 'OFFICIAL_KALSHI_FEE_ROUNDING'
   OR fee_schedule_source <>
        'https://docs.kalshi.com/getting_started/fee_rounding'

UNION ALL

SELECT
    'CAPITAL_RECOMPUTE',
    postfill_fok_outcome_id,
    'reported dollar-seconds or peak capital differs from x/f/r formula'
FROM outcome_math
WHERE ABS(
        capital_dollar_seconds - recomputed_capital_dollar_seconds
      ) > 0.000000000001
   OR synthetic_cancel_effective_elapsed_ms > fok_terminal_elapsed_ms
   OR peak_strategy_capital_safe_upper_usd <>
        GREATEST(
            locked_pair_capital_safe_upper_usd,
            first_leg_basis_safe_upper_usd
            + fok_candidate_reserve_safe_upper_usd
        )

UNION ALL

SELECT
    'FOK_CASH_RECOMPUTE',
    postfill_fok_outcome_id,
    'directional FOK principal/proceeds/fee reserve differs from state gate'
FROM outcome_math
WHERE fok_candidate_reserve_safe_upper_usd <>
        state_candidate_reserve_usd
   OR peak_strategy_capital_safe_upper_usd <>
        state_peak_capital_usd
   OR state_fee_upper_usd <
        taker_fee_partition_dp_no_rebate_upper_usd
   OR ABS(
        state_fee_upper_usd
        - (
            (requested_qty_fp / CAST(0.01 AS DECIMAL(38, 12)))
            * (
                CEIL(
                    (
                        CAST(0.07 AS DECIMAL(38, 12))
                        * CAST(0.01 AS DECIMAL(38, 12))
                        * (
                            CAST(fok_limit_price_e4 AS DECIMAL(38, 12))
                            / CAST(10000 AS DECIMAL(38, 12))
                        )
                        * (
                            CAST(1 AS DECIMAL(38, 12))
                            - CAST(
                                fok_limit_price_e4
                                AS DECIMAL(38, 12)
                              )
                              / CAST(10000 AS DECIMAL(38, 12))
                        )
                    ) * 10000
                ) / 10000
            )
        )
      ) > 0.000000000001
   OR state_cash_required_usd > simulated_available_cash_after_cancel_usd
   OR (
        fok_book_side = 'BID'
        AND state_fok_principal_usd <>
            requested_qty_fp * fok_limit_price_e4 / 10000
      )
   OR (
        fok_book_side = 'ASK'
        AND state_sale_proceeds_usd <>
            requested_qty_fp * fok_limit_price_e4 / 10000
      )
   OR state_cash_required_usd <>
        CASE
            WHEN fok_book_side = 'BID'
            THEN state_fok_principal_usd + state_fee_upper_usd
            ELSE GREATEST(
                0,
                state_fee_upper_usd - state_sale_proceeds_usd
            )
        END
   OR state_candidate_reserve_usd <> state_cash_required_usd

UNION ALL

SELECT
    'FOK_LATENCY_SCENARIO',
    postfill_fok_outcome_id,
    'FOK clocks do not implement fixed decision+60ms scenario'
FROM outcome_math
WHERE planned_effective_elapsed_ms <> decision_elapsed_ms + 60
   OR synthetic_cancel_effective_elapsed_ms <>
        decision_elapsed_ms + 60
   OR fok_terminal_elapsed_ms <> decision_elapsed_ms + 60
   OR (
        terminal_type IN ('FOK_FULL', 'FOK_ZERO')
        AND terminal_elapsed_ms <> decision_elapsed_ms + 60
      )

UNION ALL

SELECT
    'FOK_RECEIPT_CLOCK',
    postfill_fok_outcome_id,
    'synthetic cancel/FOK/settlement clocks are not causally bound'
FROM outcome_math
WHERE public_settlement_recv_wall_ns <>
        first_recv_wall_ns
        + CAST(settlement_release_elapsed_ms * 1000000 AS BIGINT)
   OR public_settlement_recv_mono_ns <>
        first_recv_mono_ns
        + CAST(settlement_release_elapsed_ms * 1000000 AS BIGINT)
   OR (
        terminal_type IN ('FOK_FULL', 'FOK_ZERO')
        AND (
            synthetic_cancel_applied_wall_ns <>
                first_recv_wall_ns
                + CAST(
                    synthetic_cancel_effective_elapsed_ms * 1000000
                    AS BIGINT
                )
            OR synthetic_cancel_applied_mono_ns <>
                first_recv_mono_ns
                + CAST(
                    synthetic_cancel_effective_elapsed_ms * 1000000
                    AS BIGINT
                )
            OR fok_processed_wall_ns <>
                first_recv_wall_ns
                + CAST(fok_terminal_elapsed_ms * 1000000 AS BIGINT)
            OR fok_processed_mono_ns <>
                first_recv_mono_ns
                + CAST(fok_terminal_elapsed_ms * 1000000 AS BIGINT)
            OR pre_effective_book_recv_wall_ns >=
                synthetic_cancel_applied_wall_ns
            OR pre_effective_book_recv_mono_ns >=
                synthetic_cancel_applied_mono_ns
            OR pre_effective_book_recv_wall_ns <
                decision_recv_wall_ns
            OR pre_effective_book_recv_mono_ns <
                decision_recv_mono_ns
        )
      )

UNION ALL

SELECT
    'KEEP_TRANSITION_SEMANTICS',
    postfill_keep_transition_id,
    'KEEP terminal/release/settlement condition is inconsistent'
FROM postfill_v41_keep_transition
WHERE (
        transition_type = 'KEEP_TO_TERMINAL'
        AND keep_terminal_type = 'COMPLEMENT_FILL'
        AND (
            interval_stop_elapsed_ms >= market_close_elapsed_ms
            OR complement_release_elapsed_ms <>
                interval_stop_elapsed_ms
        )
      )
   OR (
        transition_type = 'KEEP_TO_TERMINAL'
        AND keep_terminal_type = 'HARD_FALLBACK'
        AND (
            complement_release_elapsed_ms < market_close_elapsed_ms
            OR interval_stop_elapsed_ms <>
                settlement_release_elapsed_ms
            OR public_settlement_recv_wall_ns <>
                interval_stop_wall_ns
            OR public_settlement_recv_mono_ns <>
                interval_stop_mono_ns
        )
      )
   OR ABS(
        capital_dollar_seconds_increment
        - CASE
            WHEN interval_stop_elapsed_ms <=
                 complement_release_elapsed_ms
            THEN locked_pair_capital_safe_upper_usd
                 * (
                     interval_stop_elapsed_ms
                     - interval_start_elapsed_ms
                 ) / 1000
            WHEN interval_start_elapsed_ms >=
                 complement_release_elapsed_ms
            THEN first_leg_basis_safe_upper_usd
                 * (
                     interval_stop_elapsed_ms
                     - interval_start_elapsed_ms
                 ) / 1000
            ELSE (
                locked_pair_capital_safe_upper_usd
                * (
                    complement_release_elapsed_ms
                    - interval_start_elapsed_ms
                )
                + first_leg_basis_safe_upper_usd
                * (
                    interval_stop_elapsed_ms
                    - complement_release_elapsed_ms
                )
            ) / 1000
          END
      ) > 0.000000000001

UNION ALL

SELECT
    'SETTLEMENT_RECEIPT_DRIFT',
    o.postfill_fok_outcome_id,
    'outcome settlement receipt differs from episode KEEP receipt'
FROM postfill_v41_flatten_fok_outcome AS o
JOIN postfill_v41_keep_transition AS k
  ON k.postfill_episode_id = o.postfill_episode_id
WHERE o.source_date_utc <> k.source_date_utc
   OR o.market_ticker <> k.market_ticker
   OR o.market_id <> k.market_id
   OR o.public_settlement_receipt_id <>
        k.public_settlement_receipt_id
   OR o.public_settlement_recv_wall_ns <>
        k.public_settlement_recv_wall_ns
   OR o.public_settlement_recv_mono_ns <>
        k.public_settlement_recv_mono_ns
   OR o.public_settlement_status <> k.public_settlement_status
   OR o.official_market_result <> k.official_market_result
   OR o.public_settlement_source_rows_sha256 <>
        k.public_settlement_source_rows_sha256

UNION ALL

SELECT
    'SETTLEMENT_NORMALIZED_LINK',
    p.row_key,
    'KEEP/FOK settlement projection differs from normalized receipt'
FROM settlement_projection AS p
LEFT JOIN postfill_v41_settlement_receipt AS r
  ON r.settlement_receipt_id = p.public_settlement_receipt_id
WHERE r.settlement_receipt_id IS NULL
   OR r.source_date_utc <> p.source_date_utc
   OR r.market_ticker <> p.market_ticker
   OR r.market_id <> p.market_id
   OR r.settlement_recv_wall_ns <>
        p.public_settlement_recv_wall_ns
   OR r.settlement_recv_mono_ns <>
        p.public_settlement_recv_mono_ns
   OR r.market_status <> p.public_settlement_status
   OR r.official_market_result <> p.official_market_result
   OR r.source_rows_sha256 <>
        p.public_settlement_source_rows_sha256

UNION ALL

SELECT
    'SETTLEMENT_ZERO_LINK',
    z.postfill_episode_id,
    'zero atom is not bound to same-day normalized settlement receipt'
FROM postfill_v41_zero_time_atom AS z
LEFT JOIN postfill_v41_settlement_receipt AS r
  ON r.settlement_receipt_id = z.settlement_receipt_id
WHERE r.settlement_receipt_id IS NULL
   OR r.source_date_utc <> z.source_date_utc
   OR r.market_ticker <> z.market_ticker
   OR r.market_id <> z.market_id

UNION ALL

SELECT
    'PUBLIC_TRADE_SPINE',
    e.evidence_id,
    'proxy summary/direction/receipt is not derived from trade rows'
FROM postfill_v41_public_proxy_evidence AS e
LEFT JOIN public_trade_aggregate AS a USING (evidence_id)
WHERE COALESCE(a.trade_count, 0) = 0
   OR a.distinct_trade_index_count <> a.trade_count
   OR a.min_trade_index <> 0
   OR a.max_trade_index <> a.trade_count - 1
   OR a.cumulative_qty_fp <>
        e.derived_cumulative_strict_through_qty_fp
   OR a.cumulative_qty_fp < e.maker_order_qty_fp
   OR a.row_spine_hash_count <> 1
   OR a.identity_errors <> 0
   OR a.spine_hash_errors <> 0
   OR a.taker_semantic_errors <> 0
   OR a.receipt_order_errors <> 0
   OR a.final_public_trade_row_id <>
        e.trigger_public_trade_row_id
   OR a.final_recv_wall_ns <> e.recv_wall_ns
   OR a.final_recv_mono_ns <> e.recv_mono_ns
   OR a.final_ingest_sequence <> e.ingest_sequence
   OR a.final_stable_source_id <> e.stable_source_id

UNION ALL

SELECT
    'ORPHAN_PUBLIC_TRADE_ROW',
    t.public_trade_row_id,
    'public trade row has no proxy-evidence parent'
FROM postfill_v41_public_trade_row AS t
LEFT JOIN postfill_v41_public_proxy_evidence AS e
  ON e.evidence_id = t.evidence_id
WHERE e.evidence_id IS NULL

UNION ALL

SELECT
    'FIRST_PROXY_COVERAGE',
    s.postfill_episode_id,
    'continuous causal state must trace to exactly one first-fill proxy'
FROM (
    SELECT DISTINCT postfill_episode_id
    FROM postfill_v41_causal_state
) AS s
LEFT JOIN first_proxy_count AS p USING (postfill_episode_id)
WHERE COALESCE(p.first_proxy_count, 0) <> 1

UNION ALL

SELECT
    'FIRST_PROXY_LINK',
    s.postfill_decision_id,
    'first proxy identity/receipt does not match decision-zero state'
FROM postfill_v41_causal_state AS s
JOIN postfill_v41_public_proxy_evidence AS e
  ON e.postfill_episode_id = s.postfill_episode_id
 AND e.proxy_kind = 'FIRST_FILL'
WHERE s.decision_index = 0
  AND (
      e.source_date_utc <> s.source_date_utc
      OR e.market_ticker <> s.market_ticker
      OR e.market_id <> s.market_id
      OR e.recv_wall_ns <> s.source_max_recv_wall_ns
      OR e.recv_mono_ns <> s.source_max_recv_mono_ns
      OR e.ingest_sequence <> s.source_max_ingest_sequence
      OR e.stable_source_id <> s.source_max_stable_id
      OR e.source_rows_sha256 <> s.causal_source_rows_sha256
      OR e.market_metadata_receipt_id <>
            s.market_metadata_receipt_id
      OR e.market_metadata_source_rows_sha256 <>
            s.market_metadata_source_rows_sha256
      OR e.maker_order_outcome_side <> s.first_side
      OR e.maker_order_outcome_price_e4 <> s.first_price_e4
      OR e.maker_order_qty_fp <> s.first_qty_fp
  )

UNION ALL

SELECT
    'RACE_PROXY_LINK',
    o.postfill_fok_outcome_id,
    'race is not linked to same-market complement proxy evidence'
FROM postfill_v41_flatten_fok_outcome AS o
LEFT JOIN postfill_v41_public_proxy_evidence AS e
  ON e.evidence_id = o.race_complement_public_proxy_evidence_id
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_episode_id = o.postfill_episode_id
 AND s.decision_index = o.decision_index
WHERE o.terminal_type = 'CANCEL_RACE_PAIR'
  AND (
      e.evidence_id IS NULL
      OR s.postfill_decision_id IS NULL
      OR e.proxy_kind <> 'COMPLEMENT_FILL'
      OR e.postfill_episode_id <> o.postfill_episode_id
      OR e.market_ticker <> o.market_ticker
      OR e.market_id <> o.market_id
      OR e.maker_order_outcome_side <> s.complement_side
      OR e.maker_order_outcome_price_e4 <>
            s.complement_price_e4
      OR e.maker_order_qty_fp <> s.first_qty_fp
  )

UNION ALL

SELECT
    'KEEP_TERMINAL_EVIDENCE_LINK',
    k.postfill_keep_transition_id,
    'KEEP terminal evidence is not the matching complement/settlement'
FROM postfill_v41_keep_transition AS k
LEFT JOIN postfill_v41_causal_state AS s
  ON s.postfill_decision_id = k.from_decision_id
LEFT JOIN postfill_v41_public_proxy_evidence AS e
  ON e.evidence_id = k.terminal_public_evidence_id
WHERE k.transition_type = 'KEEP_TO_TERMINAL'
  AND (
      s.postfill_decision_id IS NULL
      OR (
          k.keep_terminal_type = 'COMPLEMENT_FILL'
          AND (
              e.evidence_id IS NULL
              OR e.proxy_kind <> 'COMPLEMENT_FILL'
              OR e.source_date_utc <> k.source_date_utc
              OR e.market_ticker <> k.market_ticker
              OR e.market_id <> k.market_id
              OR e.postfill_episode_id <> k.postfill_episode_id
              OR e.maker_order_outcome_side <> s.complement_side
              OR e.maker_order_outcome_price_e4 <>
                    s.complement_price_e4
              OR e.maker_order_qty_fp <> s.first_qty_fp
              OR e.recv_wall_ns <> k.interval_stop_wall_ns
              OR e.recv_mono_ns <> k.interval_stop_mono_ns
          )
      )
      OR (
          k.keep_terminal_type = 'HARD_FALLBACK'
          AND k.terminal_public_evidence_id <>
                k.public_settlement_receipt_id
      )
  )

UNION ALL

SELECT
    'ZERO_ATOM_PROXY_LINK',
    z.postfill_episode_id,
    'zero atom lacks same-market ordered first/complement evidence'
FROM postfill_v41_zero_time_atom AS z
LEFT JOIN postfill_v41_public_proxy_evidence AS f
  ON f.evidence_id = z.first_fill_public_proxy_evidence_id
LEFT JOIN postfill_v41_public_proxy_evidence AS c
  ON c.evidence_id = z.complement_public_proxy_evidence_id
WHERE f.evidence_id IS NULL
   OR c.evidence_id IS NULL
   OR f.proxy_kind <> 'FIRST_FILL'
   OR c.proxy_kind <> 'COMPLEMENT_FILL'
   OR f.source_date_utc <> z.source_date_utc
   OR c.source_date_utc <> z.source_date_utc
   OR f.postfill_episode_id <> z.postfill_episode_id
   OR c.postfill_episode_id <> z.postfill_episode_id
   OR f.market_ticker <> z.market_ticker
   OR c.market_ticker <> z.market_ticker
   OR f.market_id <> z.market_id
   OR c.market_id <> z.market_id
   OR f.market_metadata_receipt_id <>
        z.market_metadata_receipt_id
   OR c.market_metadata_receipt_id <>
        z.market_metadata_receipt_id
   OR f.recv_wall_ns <> z.atom_recv_wall_ns
   OR c.recv_wall_ns <> z.atom_recv_wall_ns
   OR f.recv_mono_ns <> z.atom_recv_mono_ns
   OR c.recv_mono_ns <> z.atom_recv_mono_ns
   OR f.ingest_sequence <> z.first_fill_ingest_sequence
   OR c.ingest_sequence <> z.complement_fill_ingest_sequence
   OR c.ingest_sequence <= f.ingest_sequence
   OR f.maker_order_outcome_side <> z.first_side
   OR f.maker_order_outcome_price_e4 <> z.first_price_e4
   OR f.maker_order_qty_fp <> z.first_qty_fp
   OR c.maker_order_outcome_side <> z.complement_side
   OR c.maker_order_outcome_price_e4 <> z.complement_price_e4
   OR c.maker_order_qty_fp <> z.complement_qty_fp

UNION ALL

SELECT
    'ORPHAN_PROXY_EVIDENCE',
    e.evidence_id,
    'proxy evidence is not consumed by any state/terminal/zero atom'
FROM postfill_v41_public_proxy_evidence AS e
WHERE NOT EXISTS (
        SELECT 1
        FROM postfill_v41_causal_state AS s
        WHERE e.proxy_kind = 'FIRST_FILL'
          AND s.postfill_episode_id = e.postfill_episode_id
          AND s.decision_index = 0
      )
  AND NOT EXISTS (
        SELECT 1
        FROM postfill_v41_keep_transition AS k
        WHERE k.terminal_public_evidence_id = e.evidence_id
      )
  AND NOT EXISTS (
        SELECT 1
        FROM postfill_v41_flatten_fok_outcome AS o
        WHERE o.race_complement_public_proxy_evidence_id =
              e.evidence_id
      )
  AND NOT EXISTS (
        SELECT 1
        FROM postfill_v41_zero_time_atom AS z
        WHERE z.first_fill_public_proxy_evidence_id = e.evidence_id
           OR z.complement_public_proxy_evidence_id = e.evidence_id
      )

UNION ALL

SELECT
    'ZERO_MARKET_IDENTITY_DATE',
    postfill_episode_id,
    'zero-atom ticker expiry token does not match source_date_utc'
FROM postfill_v41_zero_time_atom
WHERE SUBSTR(market_ticker, 10, 7) <>
      (
          SUBSTR(source_date_utc, 3, 2)
          || CASE SUBSTR(source_date_utc, 6, 2)
                WHEN '01' THEN 'JAN'
                WHEN '02' THEN 'FEB'
                WHEN '03' THEN 'MAR'
                WHEN '04' THEN 'APR'
                WHEN '05' THEN 'MAY'
                WHEN '06' THEN 'JUN'
                WHEN '07' THEN 'JUL'
                WHEN '08' THEN 'AUG'
                WHEN '09' THEN 'SEP'
                WHEN '10' THEN 'OCT'
                WHEN '11' THEN 'NOV'
                WHEN '12' THEN 'DEC'
             END
          || SUBSTR(source_date_utc, 9, 2)
      );
