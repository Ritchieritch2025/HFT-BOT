select(
  length == 2
  and (
    (
    (.[0] | length) == 1
    and (
      .[0][0] == "schema"
      or .[0][0] == "status"
      or .[0][0] == "candidate"
      or .[0][0] == "candidate_status"
      or .[0][0] == "validation"
      or .[0][0] == "deployable"
      or .[0][0] == "live_authorized"
      or .[0][0] == "date_2026_07_23_read"
      or .[0][0] == "date_2026_07_26_read"
    )
  )
  or (
    (.[0] | length) == 2
    and .[0][0] == "contract_receipt"
    and .[0][1] == "canonical_rows_sha256"
  )
  or (
    (.[0] | length) == 4
    and .[0][0] == "tables"
    and (
      .[0][3] == "entry_episode_id"
      or .[0][3] == "source_date_utc"
      or .[0][3] == "action_kind"
      or .[0][3] == "terminal_cause"
      or .[0][3] == "ack_state"
      or .[0][3] == "terminal_type"
      or .[0][3] == "admitted"
      or .[0][3] == "first_fill_episode_id"
      or .[0][3] == "market_ticker"
      or .[0][3] == "strict_trade_through_verified"
      or .[0][3] == "spread_e4"
      or .[0][3] == "event_yes_first"
      or .[0][3] == "event_no_first"
      or .[0][3] == "admin_censor"
      or .[0][3] == "data_invalid"
    )
  )
  )
)
