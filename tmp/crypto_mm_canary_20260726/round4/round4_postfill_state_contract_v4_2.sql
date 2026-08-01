-- ROUND4 Stage-2 V4.2 independent data-integrity schema.
--
-- V4.1 artifacts remain frozen.  V4.2 owns only the tables below and is
-- commit-authorized solely by round4_postfill_v4_2_validator.sql after an
-- exact validator SHA256 check and a second read of every source file.

CREATE TABLE postfill_v42_contract_seal (
    contract_seal_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    action_family_version VARCHAR NOT NULL,
    frozen_v41_contract_sha256 VARCHAR NOT NULL,
    exact_source_format VARCHAR NOT NULL,
    exact_source_version VARCHAR NOT NULL,
    fit_authorized BOOLEAN NOT NULL,
    candidate_selection_authorized BOOLEAN NOT NULL,
    live_authorized BOOLEAN NOT NULL,
    seal_payload_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_seal_id =
            'ROUND4_POSTFILL_V42_DATA_INTEGRITY_SEAL'
        AND contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND action_family_version =
            'ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_2'
        AND frozen_v41_contract_sha256 =
            '2642797235851cfd172ac27a2ccf1b964d65e8b3e13cbc132761fbce5db165ab'
        AND exact_source_format =
            'ROUND4_V42_EXACT_BOOK_TRADE_SOURCE_V1'
        AND exact_source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND NOT fit_authorized
        AND NOT candidate_selection_authorized
        AND NOT live_authorized
        AND seal_payload_sha256 =
            '0058e8bbd2f843f1665a03fce0b1ad7f1d5395e29795b73894184ef7d7efcdfc'
    ),
    CHECK (length(seal_payload_sha256) = 64)
);

CREATE TABLE postfill_v42_source_manifest (
    source_manifest_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    source_format VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    logical_source_path VARCHAR NOT NULL UNIQUE,
    physical_source_path VARCHAR NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    content_size_bytes BIGINT NOT NULL,
    complete BOOLEAN NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND source_format =
            'ROUND4_V42_EXACT_BOOK_TRADE_SOURCE_V1'
        AND source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND complete
        AND content_size_bytes > 0
        AND length(content_sha256) = 64
        AND length(source_manifest_id) = 64
    )
);

CREATE TABLE postfill_v42_fee_schedule_receipt (
    fee_schedule_receipt_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    v41_payload_json VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND length(payload_sha256) = 64
    )
);

CREATE TABLE postfill_v42_episode (
    postfill_episode_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    action_family_version VARCHAR NOT NULL,
    experiment_id VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    market_metadata_receipt_id VARCHAR NOT NULL UNIQUE,
    settlement_receipt_id VARCHAR NOT NULL UNIQUE,
    fee_schedule_receipt_id VARCHAR NOT NULL,
    v41_batch_sha256 VARCHAR NOT NULL,
    content_roots_sha256 VARCHAR NOT NULL,
    projection_row_count INTEGER NOT NULL,
    projection_spine_sha256 VARCHAR NOT NULL,
    zero_time_atom BOOLEAN NOT NULL,
    fit_authorized BOOLEAN NOT NULL,
    candidate_selection_authorized BOOLEAN NOT NULL,
    live_authorized BOOLEAN NOT NULL,
    UNIQUE (market_ticker, market_id),
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND action_family_version =
            'ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4_2'
        AND experiment_id =
            'KXBTC15M-ROUND4-TWO-STAGE-HAZARD-V1'
        AND length(v41_batch_sha256) = 64
        AND length(content_roots_sha256) = 64
        AND projection_row_count >= 0
        AND length(projection_spine_sha256) = 64
        AND NOT fit_authorized
        AND NOT candidate_selection_authorized
        AND NOT live_authorized
    ),
    FOREIGN KEY (fee_schedule_receipt_id)
        REFERENCES postfill_v42_fee_schedule_receipt(
            fee_schedule_receipt_id
        )
);

CREATE TABLE postfill_v42_episode_source_binding (
    episode_source_binding_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    source_manifest_id VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    content_root_sha256 VARCHAR NOT NULL,
    UNIQUE (postfill_episode_id, source_manifest_id),
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND length(content_root_sha256) = 64
    ),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id),
    FOREIGN KEY (source_manifest_id)
        REFERENCES postfill_v42_source_manifest(source_manifest_id)
);

CREATE TABLE postfill_v42_market_metadata_receipt (
    market_metadata_receipt_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL UNIQUE,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND length(payload_sha256) = 64
    ),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_settlement_receipt (
    settlement_receipt_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL UNIQUE,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    market_status VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND market_status = 'FINALIZED'
        AND length(payload_sha256) = 64
    ),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_projection_row (
    projection_row_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    projection_kind VARCHAR NOT NULL,
    original_row_key VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    v41_payload_json VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL,
    UNIQUE (projection_kind, original_row_key),
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND projection_kind IN (
            'CAUSAL_STATE',
            'ACTION',
            'KEEP_TRANSITION',
            'FLATTEN_FOK_OUTCOME',
            'FOK_SLICE',
            'PUBLIC_PROXY_EVIDENCE',
            'ZERO_TIME_ATOM'
        )
        AND length(payload_sha256) = 64
    ),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_source_record (
    source_record_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    source_manifest_id VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    source_date_utc VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    record_type VARCHAR NOT NULL,
    record_index INTEGER NOT NULL,
    payload_json VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL,
    content_root_sha256 VARCHAR NOT NULL,
    UNIQUE (source_manifest_id, record_index),
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND record_type IN ('BOOK_SNAPSHOT', 'TRADE')
        AND record_index >= 0
        AND length(payload_sha256) = 64
        AND length(content_root_sha256) = 64
    ),
    FOREIGN KEY (source_manifest_id)
        REFERENCES postfill_v42_source_manifest(source_manifest_id),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_book_snapshot (
    source_record_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    source_manifest_id VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    book_side VARCHAR NOT NULL,
    recv_wall_ns BIGINT NOT NULL,
    recv_mono_ns BIGINT NOT NULL,
    ingest_sequence BIGINT NOT NULL,
    stable_source_id VARCHAR NOT NULL,
    atomic_group_id VARCHAR NOT NULL,
    atomic_member_sequence INTEGER NOT NULL,
    atomic_group_terminal BOOLEAN NOT NULL,
    levels_json VARCHAR NOT NULL,
    v41_book_source_rows_sha256 VARCHAR NOT NULL,
    source_payload_sha256 VARCHAR NOT NULL,
    content_root_sha256 VARCHAR NOT NULL,
    row_payload_sha256 VARCHAR NOT NULL,
    UNIQUE (
        postfill_episode_id,
        decision_index,
        recv_wall_ns,
        recv_mono_ns,
        atomic_member_sequence
    ),
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND decision_index >= 0
        AND book_side IN ('BID', 'ASK')
        AND recv_wall_ns > 0
        AND recv_mono_ns > 0
        AND ingest_sequence >= 0
        AND atomic_member_sequence >= 0
        AND length(v41_book_source_rows_sha256) = 64
        AND length(source_payload_sha256) = 64
        AND length(content_root_sha256) = 64
        AND length(row_payload_sha256) = 64
    ),
    FOREIGN KEY (source_record_id)
        REFERENCES postfill_v42_source_record(source_record_id),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_fok_book_binding (
    postfill_fok_outcome_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    decision_index INTEGER NOT NULL,
    terminal_type VARCHAR NOT NULL,
    source_record_id VARCHAR NOT NULL UNIQUE,
    source_manifest_id VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    content_root_sha256 VARCHAR NOT NULL,
    source_payload_sha256 VARCHAR NOT NULL,
    effective_wall_ns BIGINT NOT NULL,
    effective_mono_ns BIGINT NOT NULL,
    selected_recv_wall_ns BIGINT NOT NULL,
    selected_recv_mono_ns BIGINT NOT NULL,
    selected_ingest_sequence BIGINT NOT NULL,
    selected_stable_source_id VARCHAR NOT NULL,
    selected_atomic_group_id VARCHAR NOT NULL,
    selected_atomic_member_sequence INTEGER NOT NULL,
    selected_v41_book_source_rows_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND terminal_type IN ('FOK_FULL', 'FOK_ZERO')
        AND decision_index >= 0
        AND selected_recv_wall_ns < effective_wall_ns
        AND selected_recv_mono_ns < effective_mono_ns
        AND length(content_root_sha256) = 64
        AND length(source_payload_sha256) = 64
        AND length(selected_v41_book_source_rows_sha256) = 64
    ),
    FOREIGN KEY (source_record_id)
        REFERENCES postfill_v42_book_snapshot(source_record_id),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_trade_row (
    public_trade_row_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    source_record_id VARCHAR NOT NULL UNIQUE,
    source_manifest_id VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    postfill_episode_id VARCHAR NOT NULL,
    market_ticker VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    trade_id VARCHAR NOT NULL,
    stable_source_id VARCHAR NOT NULL,
    recv_wall_ns BIGINT NOT NULL,
    recv_mono_ns BIGINT NOT NULL,
    ingest_sequence BIGINT NOT NULL,
    trade_payload_json VARCHAR NOT NULL,
    source_payload_sha256 VARCHAR NOT NULL,
    content_root_sha256 VARCHAR NOT NULL,
    row_payload_sha256 VARCHAR NOT NULL,
    trade_row_spine_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND source_version =
            'KXBTC15M_PUBLIC_RECEIPT_EXACT_V1'
        AND recv_wall_ns > 0
        AND recv_mono_ns > 0
        AND ingest_sequence >= 0
        AND length(source_payload_sha256) = 64
        AND length(content_root_sha256) = 64
        AND length(row_payload_sha256) = 64
        AND length(trade_row_spine_sha256) = 64
    ),
    FOREIGN KEY (source_record_id)
        REFERENCES postfill_v42_source_record(source_record_id),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_book_spine (
    postfill_episode_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    row_count INTEGER NOT NULL,
    content_roots_sha256 VARCHAR NOT NULL,
    row_spine_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND row_count >= 0
        AND length(content_roots_sha256) = 64
        AND length(row_spine_sha256) = 64
    ),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);

CREATE TABLE postfill_v42_trade_spine (
    postfill_episode_id VARCHAR PRIMARY KEY,
    contract_version VARCHAR NOT NULL,
    row_count INTEGER NOT NULL,
    content_roots_sha256 VARCHAR NOT NULL,
    row_spine_sha256 VARCHAR NOT NULL,
    CHECK (
        contract_version =
            'ROUND4_POSTFILL_PUBLIC_PROXY_V4_2'
        AND row_count >= 0
        AND length(content_roots_sha256) = 64
        AND length(row_spine_sha256) = 64
    ),
    FOREIGN KEY (postfill_episode_id)
        REFERENCES postfill_v42_episode(postfill_episode_id)
);
