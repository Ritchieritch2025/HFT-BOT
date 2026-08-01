"""Market taxonomy shared by replication experiments.

Operator doctrine (2026-07-25 audit ruling): NEVER pool across market
families — always stratify by family first and report per-family conclusions.
"""

# ---- market families (first stratification layer, never merged) ----
CRYPTO_15M = {'KXBTC15M', 'KXETH15M'}
CRYPTO_HOURLY = {'KXBTCD', 'KXETHD', 'KXBTC', 'KXETH'}
# single-match outcome series (game result, in-game info flow exists)
SPORTS_GAME_EXTRA = {'KXBOXING', 'KXUFC', 'KXMMA', 'KXATPMATCH', 'KXWTAMATCH', 'KXTENNISMATCH'}
POLITICS_CATEGORIES = {'Politics', 'Elections', 'Mentions'}

FAMILIES = ['crypto_15m', 'crypto_hourly', 'sports_game', 'sports_prop',
            'politics_events', 'econ']


def family_of(series, category):
    """Map (series_ticker, category) -> family name, or None if out of scope."""
    if series in CRYPTO_15M:
        return 'crypto_15m'
    if series in CRYPTO_HOURLY:
        return 'crypto_hourly'
    if category == 'Sports':
        if 'GAME' in str(series) or series in SPORTS_GAME_EXTRA:
            return 'sports_game'
        return 'sports_prop'
    if category in POLITICS_CATEGORIES:
        return 'politics_events'
    if category == 'Economics':
        return 'econ'
    return None


# ---- price buckets (cents, yes-axis or taker-paid axis) ----
BUCKETS = ['01-05', '05-10', '10-20', '20-35', '35-50', '50-65', '65-80', '80-90', '90-95', '95-99']
MIRROR = {'01-05': '95-99', '05-10': '90-95', '10-20': '80-90', '20-35': '65-80', '35-50': '50-65',
          '50-65': '35-50', '65-80': '20-35', '80-90': '10-20', '90-95': '05-10', '95-99': '01-05'}

# ---- time-to-resolution bands (minutes before market close) ----
BANDS = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']

# DuckDB CASE fragments; .format(x=<price col in cents>) / .format(t=<minutes col>)
BUCKET_SQL = """CASE WHEN {x} >= 1 AND {x} < 5 THEN '01-05' WHEN {x} >= 5 AND {x} < 10 THEN '05-10'
 WHEN {x} >= 10 AND {x} < 20 THEN '10-20' WHEN {x} >= 20 AND {x} < 35 THEN '20-35'
 WHEN {x} >= 35 AND {x} < 50 THEN '35-50' WHEN {x} >= 50 AND {x} < 65 THEN '50-65'
 WHEN {x} >= 65 AND {x} < 80 THEN '65-80' WHEN {x} >= 80 AND {x} < 90 THEN '80-90'
 WHEN {x} >= 90 AND {x} < 95 THEN '90-95' WHEN {x} >= 95 AND {x} <= 99 THEN '95-99' ELSE NULL END"""
BAND_SQL = """CASE WHEN {t} > 60 THEN 'gt60m' WHEN {t} > 10 THEN '10-60m' WHEN {t} > 5 THEN '5-10m'
 WHEN {t} > 2 THEN '2-5m' WHEN {t} > -5 THEN 'lt2m' ELSE NULL END"""
