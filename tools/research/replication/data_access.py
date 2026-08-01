"""Canonical access to sealed warehouse data + settlement truth.

Works on both hosts (auto-detects roots; override with env vars):
  EC2 prod : warehouse /home/ubuntu/hft-bot/work/warehouse
             catalog   /home/ubuntu/h6b_inputs/catalog_normal
  Mac      : warehouse "/Users/ritcardo/HFT BOT/work/warehouse"  (only 07-07..09 pre-cutover)

Env overrides: KALSHI_WAREHOUSE, KALSHI_CATALOG_NORMAL.

Units (recorded once, stop re-deriving them):
  *_e4 prices  : 1e-4 dollars  -> cents = e4/100  (tapered deci-cent possible)
  count_e4     : contracts x 1e4
  ts_utc       : microseconds UTC epoch
  yes+no price : sum to 10000 e4 on every trade print
"""
import os, glob, json
import datetime as _dt

_WAREHOUSE_CANDIDATES = [
    os.environ.get('KALSHI_WAREHOUSE'),
    '/home/ubuntu/hft-bot/work/warehouse',
    '/Users/ritcardo/HFT BOT/work/warehouse',
]
_CATALOG_CANDIDATES = [
    os.environ.get('KALSHI_CATALOG_NORMAL'),
    '/home/ubuntu/h6b_inputs/catalog_normal',
]


def warehouse_root():
    for p in _WAREHOUSE_CANDIDATES:
        if p and os.path.isdir(p):
            return p
    raise FileNotFoundError('no warehouse root found; set KALSHI_WAREHOUSE')


def catalog_root():
    for p in _CATALOG_CANDIDATES:
        if p and os.path.isdir(p):
            return p
    raise FileNotFoundError('no catalog_normal found; set KALSHI_CATALOG_NORMAL '
                            '(full snapshot lives on EC2 only)')


def sealed_dates():
    """Dates with a seal file — the only dates experiments should use."""
    seals = glob.glob(os.path.join(warehouse_root(), 'seals', 'date=*.json'))
    return sorted(os.path.basename(s)[5:-5] for s in seals)


def trade_files(dates=None):
    """csv.gz trade prints, hive layout category=*/subcategory=*/date=*/ ."""
    root = warehouse_root()
    fs = []
    for d in (dates or sealed_dates()):
        fs += glob.glob('%s/facts/trades/category=*/subcategory=*/date=%s/*.csv.gz' % (root, d))
    return sorted(fs)


def l1_files(dates=None):
    """parquet L1 quotes (yes_bid/ask_e4 + qty), same hive layout."""
    root = warehouse_root()
    fs = []
    for d in (dates or sealed_dates()):
        fs += glob.glob('%s/facts/orderbooks_l1/category=*/subcategory=*/date=%s/*.parquet' % (root, d))
    return sorted(fs)


def latest_catalog_snapshot():
    snaps = sorted(glob.glob(os.path.join(catalog_root(), 'snapshot=*')))
    complete = [s for s in snaps if os.path.exists(os.path.join(s, 'DONE'))]
    if not complete:
        raise FileNotFoundError('no COMPLETE catalog_normal snapshot')
    return complete[-1]


def build_settlement_index(out_parquet, snapshot=None):
    """catalog_normal shards -> parquet: ticker, series, result(yes/no),
    close_us, settle_us, anchor_us (resolution anchor = close_time, fallback
    settlement_ts). Finalized binary yes/no ONLY — scalar & unfinalized skipped.
    Returns (n_rows, skipped_scalar, skipped_unfinalized)."""
    import pandas as pd
    snap = snapshot or latest_catalog_snapshot()
    rows, n_scalar, n_unfinal = [], 0, 0
    for fp in glob.glob(os.path.join(snap, 'shards', '*.json')):
        d = json.load(open(fp))
        for t, m in d['markets'].items():
            if m.get('status') != 'finalized':
                n_unfinal += 1
                continue
            r = m.get('result')
            if r not in ('yes', 'no'):
                n_scalar += 1
                continue

            def us(k):
                v = m.get(k)
                if not v:
                    return None
                try:
                    return int(_dt.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1e6)
                except Exception:
                    return None
            rows.append((t, d['series'], r, us('close_time'), us('settlement_ts')))
    df = pd.DataFrame(rows, columns=['ticker', 'series', 'result', 'close_us', 'settle_us'])
    df = df.drop_duplicates('ticker')
    df['anchor_us'] = df['close_us'].fillna(df['settle_us'])
    df = df[df['anchor_us'].notna()]
    write_df_parquet(df, out_parquet)
    return len(df), n_scalar, n_unfinal


def write_df_parquet(df, path):
    """Write a DataFrame to parquet via DuckDB (no pyarrow on the prod venv)."""
    import duckdb
    con = duckdb.connect()
    con.register('_df', df)
    con.execute("COPY (SELECT * FROM _df) TO '%s' (FORMAT PARQUET)" % path)
    con.close()


def read_parquet_df(path):
    """Read parquet to DataFrame via DuckDB (no pyarrow on the prod venv)."""
    import duckdb
    con = duckdb.connect()
    df = con.execute("SELECT * FROM read_parquet('%s')" % path).df()
    con.close()
    return df


def duckdb_connect(threads=2, memory_limit='8GB', temp_dir=None):
    """Prod-box-safe DuckDB connection (capped threads/memory)."""
    import duckdb
    con = duckdb.connect()
    con.execute('PRAGMA threads=%d' % threads)
    con.execute("PRAGMA memory_limit='%s'" % memory_limit)
    con.execute('SET preserve_insertion_order=false')
    if temp_dir:
        con.execute("PRAGMA temp_directory='%s'" % temp_dir)
    return con


def sql_file_list(files):
    """Inline a python list of paths as a DuckDB list literal (table functions
    cannot take prepared-statement parameters)."""
    return '[' + ','.join("'%s'" % f for f in files) + ']'
