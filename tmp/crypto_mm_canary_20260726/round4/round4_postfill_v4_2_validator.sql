-- ROUND4 V4.2 normative commit validator.
--
-- The table builder refuses to execute any bytes whose SHA256 differs from
-- its pinned NORMATIVE_VALIDATOR_SHA256.  This script is intentionally
-- read-only with respect to permanent V4.2 rows: it creates two TEMP views.

CREATE OR REPLACE TEMP VIEW postfill_v42_validator_attestation AS
SELECT
    'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'::VARCHAR AS contract_version,
    validator_sha256::VARCHAR AS validator_sha256
FROM postfill_v42_validator_runtime_input;

CREATE OR REPLACE TEMP VIEW postfill_v42_validation_violations AS
WITH
episode_roots AS (
    SELECT
        postfill_episode_id,
        sha256(
            coalesce(
                string_agg(
                    DISTINCT content_root_sha256,
                    '|' ORDER BY content_root_sha256
                ),
                ''
            )
        ) AS content_roots_sha256
    FROM postfill_v42_source_record
    GROUP BY postfill_episode_id
),
projection_spines AS (
    SELECT
        postfill_episode_id,
        count(*)::BIGINT AS row_count,
        sha256(
            coalesce(
                string_agg(
                    projection_kind || ':' ||
                    original_row_key || ':' ||
                    payload_sha256,
                    '|' ORDER BY projection_kind, original_row_key
                ),
                ''
            )
        ) AS row_spine_sha256
    FROM postfill_v42_projection_row
    GROUP BY postfill_episode_id
),
book_spines AS (
    SELECT
        postfill_episode_id,
        count(*)::BIGINT AS row_count,
        sha256(
            coalesce(
                string_agg(
                    content_root_sha256 || ':' ||
                    source_record_id || ':' ||
                    source_payload_sha256,
                    '|' ORDER BY
                        source_manifest_id,
                        recv_wall_ns,
                        recv_mono_ns,
                        ingest_sequence,
                        source_record_id
                ),
                ''
            )
        ) AS row_spine_sha256
    FROM postfill_v42_book_snapshot
    GROUP BY postfill_episode_id
),
trade_spines AS (
    SELECT
        postfill_episode_id,
        count(*)::BIGINT AS row_count,
        sha256(
            coalesce(
                string_agg(
                    content_root_sha256 || ':' ||
                    source_record_id || ':' ||
                    source_payload_sha256,
                    '|' ORDER BY
                        source_manifest_id,
                        recv_wall_ns,
                        recv_mono_ns,
                        ingest_sequence,
                        source_record_id
                ),
                ''
            )
        ) AS row_spine_sha256
    FROM postfill_v42_trade_row
    GROUP BY postfill_episode_id
),
book_atomic_groups AS (
    SELECT
        postfill_episode_id,
        decision_index,
        recv_wall_ns,
        recv_mono_ns,
        count(*)::BIGINT AS member_count,
        count(DISTINCT atomic_group_id)::BIGINT AS group_id_count,
        count(DISTINCT atomic_member_sequence)::BIGINT AS sequence_count,
        min(atomic_member_sequence)::BIGINT AS min_sequence,
        max(atomic_member_sequence)::BIGINT AS max_sequence,
        sum(
            CASE WHEN atomic_group_terminal THEN 1 ELSE 0 END
        )::BIGINT AS terminal_count,
        max(
            CASE
                WHEN atomic_group_terminal
                THEN atomic_member_sequence
                ELSE -1
            END
        )::BIGINT AS terminal_sequence
    FROM postfill_v42_book_snapshot
    GROUP BY
        postfill_episode_id,
        decision_index,
        recv_wall_ns,
        recv_mono_ns
),
eligible_books AS (
    SELECT
        binding.postfill_fok_outcome_id,
        book.source_record_id,
        row_number() OVER (
            PARTITION BY binding.postfill_fok_outcome_id
            ORDER BY
                book.recv_wall_ns DESC,
                book.recv_mono_ns DESC,
                book.ingest_sequence DESC,
                book.source_record_id DESC
        ) AS latest_rank
    FROM postfill_v42_fok_book_binding AS binding
    JOIN postfill_v42_book_snapshot AS book
      ON book.postfill_episode_id = binding.postfill_episode_id
     AND book.decision_index = binding.decision_index
     AND book.atomic_group_terminal
     AND book.recv_wall_ns < binding.effective_wall_ns
     AND book.recv_mono_ns < binding.effective_mono_ns
),
projection_counts AS (
    SELECT
        postfill_episode_id,
        sum(
            CASE WHEN projection_kind = 'CAUSAL_STATE' THEN 1 ELSE 0 END
        )::BIGINT AS causal_count,
        sum(
            CASE WHEN projection_kind = 'ACTION' THEN 1 ELSE 0 END
        )::BIGINT AS action_count,
        sum(
            CASE WHEN projection_kind = 'KEEP_TRANSITION' THEN 1 ELSE 0 END
        )::BIGINT AS keep_count,
        sum(
            CASE
                WHEN projection_kind = 'FLATTEN_FOK_OUTCOME'
                THEN 1 ELSE 0
            END
        )::BIGINT AS outcome_count,
        sum(
            CASE WHEN projection_kind = 'FOK_SLICE' THEN 1 ELSE 0 END
        )::BIGINT AS slice_count,
        sum(
            CASE
                WHEN projection_kind = 'PUBLIC_PROXY_EVIDENCE'
                THEN 1 ELSE 0
            END
        )::BIGINT AS evidence_count,
        sum(
            CASE WHEN projection_kind = 'ZERO_TIME_ATOM' THEN 1 ELSE 0 END
        )::BIGINT AS zero_count
    FROM postfill_v42_projection_row
    GROUP BY postfill_episode_id
),
slice_coverage AS (
    SELECT
        postfill_episode_id,
        json_extract_string(
            v41_payload_json,
            '$.postfill_fok_outcome_id'
        ) AS postfill_fok_outcome_id,
        count(*)::BIGINT AS slice_count,
        count(
            DISTINCT CAST(
                json_extract(v41_payload_json, '$.slice_index')
                AS INTEGER
            )
        )::BIGINT AS distinct_index_count,
        min(
            CAST(
                json_extract(v41_payload_json, '$.slice_index')
                AS INTEGER
            )
        )::BIGINT AS min_slice_index,
        max(
            CAST(
                json_extract(v41_payload_json, '$.slice_index')
                AS INTEGER
            )
        )::BIGINT AS max_slice_index,
        sum(
            CAST(
                json_extract_string(
                    v41_payload_json,
                    '$.slice_qty_fp'
                ) AS DECIMAL(38, 12)
            )
        ) AS slice_qty_fp
    FROM postfill_v42_projection_row
    WHERE projection_kind = 'FOK_SLICE'
    GROUP BY
        postfill_episode_id,
        json_extract_string(
            v41_payload_json,
            '$.postfill_fok_outcome_id'
        )
)
SELECT
    'CONTRACT_SEAL_CARDINALITY'::VARCHAR AS violation_code,
    'BATCH'::VARCHAR AS row_key,
    ('count=' || count(*))::VARCHAR AS detail
FROM postfill_v42_contract_seal
HAVING count(*) <> 1

UNION ALL
SELECT
    'FEE_RECEIPT_CARDINALITY',
    'BATCH',
    'count=' || count(*)
FROM postfill_v42_fee_schedule_receipt
HAVING count(*) <> 1

UNION ALL
SELECT
    'FEE_PAYLOAD_HASH',
    fee_schedule_receipt_id,
    'payload SHA mismatch'
FROM postfill_v42_fee_schedule_receipt
WHERE sha256(v41_payload_json) <> payload_sha256

UNION ALL
SELECT
    'EPISODE_AUTHORIZATION',
    postfill_episode_id,
    'FIT/CANDIDATE/LIVE must remain false'
FROM postfill_v42_episode
WHERE
    fit_authorized
    OR candidate_selection_authorized
    OR live_authorized

UNION ALL
SELECT
    'EPISODE_SOURCE_CLOSURE',
    episode.postfill_episode_id,
    'missing binding or content-root spine mismatch'
FROM postfill_v42_episode AS episode
LEFT JOIN episode_roots AS roots
  ON roots.postfill_episode_id = episode.postfill_episode_id
WHERE
    roots.postfill_episode_id IS NULL
    OR roots.content_roots_sha256 <> episode.content_roots_sha256
    OR NOT EXISTS (
        SELECT 1
        FROM postfill_v42_episode_source_binding AS binding
        WHERE
            binding.postfill_episode_id =
                episode.postfill_episode_id
    )

UNION ALL
SELECT
    'SOURCE_MANIFEST_ORPHAN',
    manifest.source_manifest_id,
    'manifest is not referenced by a legal episode'
FROM postfill_v42_source_manifest AS manifest
WHERE NOT EXISTS (
    SELECT 1
    FROM postfill_v42_episode_source_binding AS binding
    WHERE binding.source_manifest_id = manifest.source_manifest_id
)

UNION ALL
SELECT
    'SOURCE_BINDING_MISMATCH',
    binding.episode_source_binding_id,
    'version/content root/date closure mismatch'
FROM postfill_v42_episode_source_binding AS binding
JOIN postfill_v42_source_manifest AS manifest
  ON manifest.source_manifest_id = binding.source_manifest_id
JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = binding.postfill_episode_id
WHERE
    binding.source_version <> manifest.source_version
    OR binding.content_root_sha256 <> manifest.content_sha256
    OR episode.source_date_utc <> manifest.source_date_utc
    OR NOT EXISTS (
        SELECT 1
        FROM postfill_v42_source_record AS source_record
        WHERE
            source_record.postfill_episode_id =
                binding.postfill_episode_id
            AND source_record.source_manifest_id =
                binding.source_manifest_id
    )

UNION ALL
SELECT
    'METADATA_MISSING_OR_MISMATCH',
    episode.postfill_episode_id,
    'episode must reference exactly its own matching metadata receipt'
FROM postfill_v42_episode AS episode
LEFT JOIN postfill_v42_market_metadata_receipt AS metadata
  ON metadata.market_metadata_receipt_id =
        episode.market_metadata_receipt_id
WHERE
    metadata.market_metadata_receipt_id IS NULL
    OR metadata.postfill_episode_id <> episode.postfill_episode_id
    OR metadata.source_date_utc <> episode.source_date_utc
    OR metadata.market_ticker <> episode.market_ticker
    OR metadata.market_id <> episode.market_id

UNION ALL
SELECT
    'METADATA_ORPHAN_OR_PAYLOAD',
    metadata.market_metadata_receipt_id,
    'orphan/mismatched metadata or payload SHA'
FROM postfill_v42_market_metadata_receipt AS metadata
LEFT JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = metadata.postfill_episode_id
WHERE
    episode.postfill_episode_id IS NULL
    OR episode.market_metadata_receipt_id <>
        metadata.market_metadata_receipt_id
    OR metadata.source_date_utc <> episode.source_date_utc
    OR metadata.market_ticker <> episode.market_ticker
    OR metadata.market_id <> episode.market_id
    OR sha256(metadata.payload_json) <> metadata.payload_sha256

UNION ALL
SELECT
    'SETTLEMENT_MISSING_OR_MISMATCH',
    episode.postfill_episode_id,
    'episode must reference exactly its own FINALIZED receipt'
FROM postfill_v42_episode AS episode
LEFT JOIN postfill_v42_settlement_receipt AS settlement
  ON settlement.settlement_receipt_id =
        episode.settlement_receipt_id
WHERE
    settlement.settlement_receipt_id IS NULL
    OR settlement.postfill_episode_id <> episode.postfill_episode_id
    OR settlement.source_date_utc <> episode.source_date_utc
    OR settlement.market_ticker <> episode.market_ticker
    OR settlement.market_id <> episode.market_id
    OR settlement.market_status <> 'FINALIZED'

UNION ALL
SELECT
    'SETTLEMENT_ORPHAN_OR_PAYLOAD',
    settlement.settlement_receipt_id,
    'orphan/mismatched settlement or payload SHA'
FROM postfill_v42_settlement_receipt AS settlement
LEFT JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = settlement.postfill_episode_id
WHERE
    episode.postfill_episode_id IS NULL
    OR episode.settlement_receipt_id <>
        settlement.settlement_receipt_id
    OR settlement.source_date_utc <> episode.source_date_utc
    OR settlement.market_ticker <> episode.market_ticker
    OR settlement.market_id <> episode.market_id
    OR sha256(settlement.payload_json) <> settlement.payload_sha256

UNION ALL
SELECT
    'SOURCE_RECORD_CLOSURE',
    source_record.source_record_id,
    'manifest/episode/version/root/payload mismatch'
FROM postfill_v42_source_record AS source_record
JOIN postfill_v42_source_manifest AS manifest
  ON manifest.source_manifest_id = source_record.source_manifest_id
JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = source_record.postfill_episode_id
WHERE
    source_record.source_version <> manifest.source_version
    OR source_record.source_date_utc <> manifest.source_date_utc
    OR source_record.content_root_sha256 <> manifest.content_sha256
    OR sha256(source_record.payload_json) <>
        source_record.payload_sha256
    OR NOT EXISTS (
        SELECT 1
        FROM postfill_v42_episode_source_binding AS binding
        WHERE
            binding.postfill_episode_id =
                source_record.postfill_episode_id
            AND binding.source_manifest_id =
                source_record.source_manifest_id
            AND binding.content_root_sha256 =
                source_record.content_root_sha256
    )

UNION ALL
SELECT
    'SOURCE_RECORD_TYPED_CLOSURE',
    source_record.source_record_id,
    'BOOK/TRADE record must have exactly one typed row'
FROM postfill_v42_source_record AS source_record
WHERE
    (
        source_record.record_type = 'BOOK_SNAPSHOT'
        AND (
            SELECT count(*)
            FROM postfill_v42_book_snapshot AS book
            WHERE book.source_record_id =
                source_record.source_record_id
        ) <> 1
    )
    OR (
        source_record.record_type = 'TRADE'
        AND (
            SELECT count(*)
            FROM postfill_v42_trade_row AS trade
            WHERE trade.source_record_id =
                source_record.source_record_id
        ) <> 1
    )

UNION ALL
SELECT
    'BOOK_SOURCE_CLOSURE',
    book.source_record_id,
    'book row differs from source record/manifest/episode'
FROM postfill_v42_book_snapshot AS book
JOIN postfill_v42_source_record AS source_record
  ON source_record.source_record_id = book.source_record_id
JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = book.postfill_episode_id
WHERE
    source_record.record_type <> 'BOOK_SNAPSHOT'
    OR book.source_manifest_id <> source_record.source_manifest_id
    OR book.source_version <> source_record.source_version
    OR book.source_payload_sha256 <> source_record.payload_sha256
    OR book.content_root_sha256 <> source_record.content_root_sha256
    OR book.postfill_episode_id <> source_record.postfill_episode_id
    OR book.market_ticker <> episode.market_ticker
    OR book.market_id <> episode.market_id
    OR json_extract_string(
        source_record.payload_json,
        '$.postfill_episode_id'
    ) <> book.postfill_episode_id
    OR CAST(
        json_extract(source_record.payload_json, '$.decision_index')
        AS INTEGER
    ) <> book.decision_index
    OR json_extract_string(
        source_record.payload_json,
        '$.book_side'
    ) <> book.book_side
    OR json_extract_string(
        source_record.payload_json,
        '$.atomic_group_id'
    ) <> book.atomic_group_id
    OR CAST(
        json_extract(
            source_record.payload_json,
            '$.atomic_member_sequence'
        ) AS INTEGER
    ) <> book.atomic_member_sequence
    OR CAST(
        json_extract(
            source_record.payload_json,
            '$.atomic_group_terminal'
        ) AS BOOLEAN
    ) <> book.atomic_group_terminal
    OR json_extract_string(
        source_record.payload_json,
        '$.book.market_ticker'
    ) <> book.market_ticker
    OR json_extract_string(
        source_record.payload_json,
        '$.book.market_id'
    ) <> book.market_id
    OR CAST(
        json_extract(
            source_record.payload_json,
            '$.book.recv_wall_ns'
        ) AS BIGINT
    ) <> book.recv_wall_ns
    OR CAST(
        json_extract(
            source_record.payload_json,
            '$.book.recv_mono_ns'
        ) AS BIGINT
    ) <> book.recv_mono_ns
    OR CAST(
        json_extract(
            source_record.payload_json,
            '$.book.ingest_sequence'
        ) AS BIGINT
    ) <> book.ingest_sequence
    OR json_extract_string(
        source_record.payload_json,
        '$.book.stable_source_id'
    ) <> book.stable_source_id
    OR json_extract_string(
        source_record.payload_json,
        '$.book.levels'
    ) <> book.levels_json
    OR json_extract_string(
        source_record.payload_json,
        '$.book.source_rows_sha256'
    ) <> book.v41_book_source_rows_sha256

UNION ALL
SELECT
    'BOOK_ROW_HASH',
    source_record_id,
    'book row payload SHA mismatch'
FROM postfill_v42_book_snapshot
WHERE
    sha256(
        concat_ws(
            chr(31),
            source_record_id,
            source_manifest_id,
            postfill_episode_id,
            decision_index::VARCHAR,
            market_ticker,
            market_id,
            book_side,
            recv_wall_ns::VARCHAR,
            recv_mono_ns::VARCHAR,
            ingest_sequence::VARCHAR,
            stable_source_id,
            atomic_group_id,
            atomic_member_sequence::VARCHAR,
            CASE
                WHEN atomic_group_terminal THEN 'true'
                ELSE 'false'
            END,
            levels_json,
            v41_book_source_rows_sha256,
            source_payload_sha256,
            content_root_sha256
        )
    ) <> row_payload_sha256

UNION ALL
SELECT
    'BOOK_ATOMIC_ENVELOPE',
    postfill_episode_id || ':' || decision_index::VARCHAR ||
        ':' || recv_wall_ns::VARCHAR || ':' || recv_mono_ns::VARCHAR,
    'same receive-clock must be one contiguous atomic envelope'
FROM book_atomic_groups
WHERE
    group_id_count <> 1
    OR sequence_count <> member_count
    OR min_sequence <> 0
    OR max_sequence <> member_count - 1
    OR terminal_count <> 1
    OR terminal_sequence <> max_sequence

UNION ALL
SELECT
    'BOOK_WITHOUT_FOK_DECISION',
    book.source_record_id,
    'book row has no non-race FOK outcome'
FROM postfill_v42_book_snapshot AS book
WHERE NOT EXISTS (
    SELECT 1
    FROM postfill_v42_projection_row AS outcome
    WHERE
        outcome.projection_kind = 'FLATTEN_FOK_OUTCOME'
        AND outcome.postfill_episode_id =
            book.postfill_episode_id
        AND CAST(
            json_extract(
                outcome.v41_payload_json,
                '$.decision_index'
            ) AS INTEGER
        ) = book.decision_index
        AND json_extract_string(
            outcome.v41_payload_json,
            '$.terminal_type'
        ) <> 'CANCEL_RACE_PAIR'
)

UNION ALL
SELECT
    'FOK_BINDING_COVERAGE',
    outcome.projection_row_id,
    'non-race outcome needs one binding; race needs none'
FROM postfill_v42_projection_row AS outcome
WHERE
    outcome.projection_kind = 'FLATTEN_FOK_OUTCOME'
    AND (
        (
            json_extract_string(
                outcome.v41_payload_json,
                '$.terminal_type'
            ) = 'CANCEL_RACE_PAIR'
            AND EXISTS (
                SELECT 1
                FROM postfill_v42_fok_book_binding AS binding
                WHERE
                    binding.postfill_fok_outcome_id =
                        outcome.original_row_key
            )
        )
        OR (
            json_extract_string(
                outcome.v41_payload_json,
                '$.terminal_type'
            ) <> 'CANCEL_RACE_PAIR'
            AND (
                SELECT count(*)
                FROM postfill_v42_fok_book_binding AS binding
                WHERE
                    binding.postfill_fok_outcome_id =
                        outcome.original_row_key
            ) <> 1
        )
    )

UNION ALL
SELECT
    'FOK_SLICE_COVERAGE',
    outcome.original_row_key,
    'slice count/index/quantity does not conserve FOK outcome'
FROM postfill_v42_projection_row AS outcome
LEFT JOIN slice_coverage AS slices
  ON slices.postfill_episode_id = outcome.postfill_episode_id
 AND slices.postfill_fok_outcome_id = outcome.original_row_key
WHERE
    outcome.projection_kind = 'FLATTEN_FOK_OUTCOME'
    AND (
        (
            json_extract_string(
                outcome.v41_payload_json,
                '$.terminal_type'
            ) = 'FOK_FULL'
            AND (
                coalesce(slices.slice_count, 0) < 1
                OR slices.distinct_index_count <> slices.slice_count
                OR slices.min_slice_index <> 0
                OR slices.max_slice_index <> slices.slice_count - 1
                OR slices.slice_qty_fp <> CAST(
                    json_extract_string(
                        outcome.v41_payload_json,
                        '$.fok_fill_qty_fp'
                    ) AS DECIMAL(38, 12)
                )
            )
        )
        OR (
            json_extract_string(
                outcome.v41_payload_json,
                '$.terminal_type'
            ) IN ('FOK_ZERO', 'CANCEL_RACE_PAIR')
            AND coalesce(slices.slice_count, 0) <> 0
        )
    )

UNION ALL
SELECT
    'FOK_SLICE_ORPHAN_OR_MISMATCH',
    slice.projection_row_id,
    'slice has no same-episode outcome/decision'
FROM postfill_v42_projection_row AS slice
LEFT JOIN postfill_v42_projection_row AS outcome
  ON outcome.projection_kind = 'FLATTEN_FOK_OUTCOME'
 AND outcome.postfill_episode_id = slice.postfill_episode_id
 AND outcome.original_row_key = json_extract_string(
        slice.v41_payload_json,
        '$.postfill_fok_outcome_id'
    )
WHERE
    slice.projection_kind = 'FOK_SLICE'
    AND (
        outcome.projection_row_id IS NULL
        OR CAST(
            json_extract(
                outcome.v41_payload_json,
                '$.decision_index'
            ) AS INTEGER
        ) <> CAST(
            json_extract(
                slice.v41_payload_json,
                '$.decision_index'
            ) AS INTEGER
        )
    )

UNION ALL
SELECT
    'FOK_BINDING_PROJECTION',
    binding.postfill_fok_outcome_id,
    'binding differs from projected outcome clocks/identity'
FROM postfill_v42_fok_book_binding AS binding
LEFT JOIN postfill_v42_projection_row AS outcome
  ON outcome.projection_kind = 'FLATTEN_FOK_OUTCOME'
 AND outcome.original_row_key = binding.postfill_fok_outcome_id
WHERE
    outcome.projection_row_id IS NULL
    OR outcome.postfill_episode_id <> binding.postfill_episode_id
    OR CAST(
        json_extract(outcome.v41_payload_json, '$.decision_index')
        AS INTEGER
    ) <> binding.decision_index
    OR json_extract_string(
        outcome.v41_payload_json,
        '$.terminal_type'
    ) <> binding.terminal_type
    OR CAST(
        json_extract(
            outcome.v41_payload_json,
            '$.fok_processed_wall_ns'
        ) AS BIGINT
    ) <> binding.effective_wall_ns
    OR CAST(
        json_extract(
            outcome.v41_payload_json,
            '$.fok_processed_mono_ns'
        ) AS BIGINT
    ) <> binding.effective_mono_ns
    OR json_extract_string(
        outcome.v41_payload_json,
        '$.pre_effective_book_stable_source_id'
    ) <> binding.selected_stable_source_id
    OR json_extract_string(
        outcome.v41_payload_json,
        '$.pre_effective_book_source_rows_sha256'
    ) <> binding.selected_v41_book_source_rows_sha256

UNION ALL
SELECT
    'FOK_BINDING_SELECTED_BOOK',
    binding.postfill_fok_outcome_id,
    'binding does not equal selected atomic book row'
FROM postfill_v42_fok_book_binding AS binding
LEFT JOIN postfill_v42_book_snapshot AS book
  ON book.source_record_id = binding.source_record_id
WHERE
    book.source_record_id IS NULL
    OR NOT book.atomic_group_terminal
    OR book.postfill_episode_id <> binding.postfill_episode_id
    OR book.decision_index <> binding.decision_index
    OR book.source_manifest_id <> binding.source_manifest_id
    OR book.source_payload_sha256 <> binding.source_payload_sha256
    OR book.content_root_sha256 <> binding.content_root_sha256
    OR book.recv_wall_ns <> binding.selected_recv_wall_ns
    OR book.recv_mono_ns <> binding.selected_recv_mono_ns
    OR book.ingest_sequence <> binding.selected_ingest_sequence
    OR book.stable_source_id <> binding.selected_stable_source_id
    OR book.atomic_group_id <> binding.selected_atomic_group_id
    OR book.atomic_member_sequence <>
        binding.selected_atomic_member_sequence
    OR book.v41_book_source_rows_sha256 <>
        binding.selected_v41_book_source_rows_sha256

UNION ALL
SELECT
    'FOK_LATEST_ASOF',
    binding.postfill_fok_outcome_id,
    'selected book is stale or no eligible terminal exists'
FROM postfill_v42_fok_book_binding AS binding
LEFT JOIN eligible_books AS eligible
  ON eligible.postfill_fok_outcome_id =
        binding.postfill_fok_outcome_id
 AND eligible.latest_rank = 1
WHERE
    eligible.source_record_id IS NULL
    OR eligible.source_record_id <> binding.source_record_id

UNION ALL
SELECT
    'TRADE_SOURCE_CLOSURE',
    trade.public_trade_row_id,
    'trade row differs from source record/manifest/episode'
FROM postfill_v42_trade_row AS trade
JOIN postfill_v42_source_record AS source_record
  ON source_record.source_record_id = trade.source_record_id
JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = trade.postfill_episode_id
WHERE
    source_record.record_type <> 'TRADE'
    OR trade.source_manifest_id <> source_record.source_manifest_id
    OR trade.source_version <> source_record.source_version
    OR trade.source_payload_sha256 <> source_record.payload_sha256
    OR trade.content_root_sha256 <> source_record.content_root_sha256
    OR trade.postfill_episode_id <> source_record.postfill_episode_id
    OR trade.market_ticker <> episode.market_ticker
    OR trade.market_id <> episode.market_id
    OR json_extract_string(
        source_record.payload_json,
        '$.postfill_episode_id'
    ) <> trade.postfill_episode_id
    OR json_extract_string(
        source_record.payload_json,
        '$.market_ticker'
    ) <> trade.market_ticker
    OR json_extract_string(
        source_record.payload_json,
        '$.market_id'
    ) <> trade.market_id
    OR json_extract_string(
        source_record.payload_json,
        '$.trade'
    ) <> trade.trade_payload_json
    OR json_extract_string(
        trade.trade_payload_json,
        '$.public_trade_row_id'
    ) <> trade.public_trade_row_id
    OR json_extract_string(
        trade.trade_payload_json,
        '$.trade_id'
    ) <> trade.trade_id
    OR json_extract_string(
        trade.trade_payload_json,
        '$.stable_source_id'
    ) <> trade.stable_source_id

UNION ALL
SELECT
    'TRADE_ROW_HASH',
    public_trade_row_id,
    'trade row payload SHA mismatch'
FROM postfill_v42_trade_row
WHERE
    sha256(
        concat_ws(
            chr(31),
            public_trade_row_id,
            source_record_id,
            source_manifest_id,
            postfill_episode_id,
            market_ticker,
            market_id,
            trade_id,
            stable_source_id,
            recv_wall_ns::VARCHAR,
            recv_mono_ns::VARCHAR,
            ingest_sequence::VARCHAR,
            trade_payload_json,
            source_payload_sha256,
            content_root_sha256
        )
    ) <> row_payload_sha256

UNION ALL
SELECT
    'PROJECTION_CLOSURE',
    projection.projection_row_id,
    'projection episode/market/date/payload mismatch'
FROM postfill_v42_projection_row AS projection
LEFT JOIN postfill_v42_episode AS episode
  ON episode.postfill_episode_id = projection.postfill_episode_id
WHERE
    episode.postfill_episode_id IS NULL
    OR projection.source_date_utc <> episode.source_date_utc
    OR projection.market_ticker <> episode.market_ticker
    OR projection.market_id <> episode.market_id
    OR sha256(projection.v41_payload_json) <>
        projection.payload_sha256

UNION ALL
SELECT
    'PROJECTION_SPINE',
    episode.postfill_episode_id,
    'projection count/spine mismatch'
FROM postfill_v42_episode AS episode
LEFT JOIN projection_spines AS spine
  ON spine.postfill_episode_id = episode.postfill_episode_id
WHERE
    coalesce(spine.row_count, 0) <> episode.projection_row_count
    OR coalesce(
        spine.row_spine_sha256,
        sha256('')
    ) <> episode.projection_spine_sha256

UNION ALL
SELECT
    'PROJECTION_BRANCH_COVERAGE',
    episode.postfill_episode_id,
    'continuous/zero-time normalized branch is incomplete'
FROM postfill_v42_episode AS episode
LEFT JOIN projection_counts AS counts
  ON counts.postfill_episode_id = episode.postfill_episode_id
WHERE
    (
        episode.zero_time_atom
        AND (
            coalesce(counts.zero_count, 0) <> 1
            OR coalesce(counts.causal_count, 0) <> 0
            OR coalesce(counts.action_count, 0) <> 0
            OR coalesce(counts.keep_count, 0) <> 0
            OR coalesce(counts.outcome_count, 0) <> 0
        )
    )
    OR (
        NOT episode.zero_time_atom
        AND (
            coalesce(counts.zero_count, 0) <> 0
            OR coalesce(counts.causal_count, 0) < 1
            OR counts.action_count <> 2 * counts.causal_count
            OR counts.keep_count <> counts.causal_count
            OR counts.outcome_count <> counts.causal_count
            OR coalesce(counts.evidence_count, 0) < 1
        )
    )

UNION ALL
SELECT
    'BOOK_SPINE',
    episode.postfill_episode_id,
    'book row count/root/spine mismatch'
FROM postfill_v42_episode AS episode
LEFT JOIN postfill_v42_book_spine AS declared
  ON declared.postfill_episode_id = episode.postfill_episode_id
LEFT JOIN book_spines AS calculated
  ON calculated.postfill_episode_id = episode.postfill_episode_id
WHERE
    declared.postfill_episode_id IS NULL
    OR declared.row_count <> coalesce(calculated.row_count, 0)
    OR declared.content_roots_sha256 <>
        episode.content_roots_sha256
    OR declared.row_spine_sha256 <>
        coalesce(calculated.row_spine_sha256, sha256(''))

UNION ALL
SELECT
    'TRADE_SPINE',
    episode.postfill_episode_id,
    'trade row count/root/spine mismatch'
FROM postfill_v42_episode AS episode
LEFT JOIN postfill_v42_trade_spine AS declared
  ON declared.postfill_episode_id = episode.postfill_episode_id
LEFT JOIN trade_spines AS calculated
  ON calculated.postfill_episode_id = episode.postfill_episode_id
WHERE
    declared.postfill_episode_id IS NULL
    OR declared.row_count <> coalesce(calculated.row_count, 0)
    OR declared.content_roots_sha256 <>
        episode.content_roots_sha256
    OR declared.row_spine_sha256 <>
        coalesce(calculated.row_spine_sha256, sha256(''))

UNION ALL
SELECT
    'TRADE_SPINE_ROW_BINDING',
    trade.public_trade_row_id,
    'trade row does not carry its episode spine'
FROM postfill_v42_trade_row AS trade
JOIN postfill_v42_trade_spine AS spine
  ON spine.postfill_episode_id = trade.postfill_episode_id
WHERE trade.trade_row_spine_sha256 <> spine.row_spine_sha256;
