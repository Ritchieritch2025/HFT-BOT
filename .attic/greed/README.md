# Greed Schema Snapshot

`schema_snapshot.json` pins the public table names and public column order used
by the local research warehouse. The warehouse converter validates emitted
Parquet files against this file and does not add internal metadata columns to
public tables.

This is a compatibility baseline for the cold-path warehouse only. Raw
`WsRecorder` NDJSON remains the truth source and the Parquet warehouse is
derived, rebuildable state.
