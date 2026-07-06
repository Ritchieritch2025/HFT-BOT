#!/usr/bin/env python3
"""Build the pinned classification dimension for the warehouse.

Correct-API-first: the reliable, Kalshi-provided levels are
  category -> subcategory(series.tags[0]) -> series_ticker -> event -> market.
Kalshi does NOT expose "league" or "region" as fields (verified) — they live in
the series_ticker / title. So this tool DERIVES a best-effort `group` (league for
sports, region for weather, asset for crypto = the tag) and emits a review CSV so
the mapping can be pinned by hand. Nothing here is guessed silently: every derived
group carries a `group_source` and low-confidence rows are flagged.

Outputs:
  warehouse/catalog/series_classified/part-00000.parquet   (pinned dim)
  config/classification_review.csv                          (for manual review)
  config/series_tags_report.csv                             (per-series full tags)

Reads config/market_classes.yaml (category -> class). Warns on any live category
missing from the config (treated as Class B).

stdlib + duckdb only.
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile

# Known sports leagues (token right after KX). Longest-match wins. Extend freely.
LEAGUES = [
    "NFL", "NCAAFB", "NCAAF", "NBA", "WNBA", "NCAABB", "NCAAMB", "NCAAWB",
    "MLB", "KBO", "NPB", "LMB", "WBC", "NHL", "MLS", "EPL", "UCL", "UECL",
    "UEL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "WTA", "ATP", "UFC",
    "PGA", "LPGA", "F1", "NASCAR", "INDYCAR", "MLR",
]
# Weather series are KXHIGH/KXHIGHT/KXLOW + city code; keep the city token as region.
WEATHER_RE = re.compile(r"^KX(?:HIGHT?|LOWT?|TEMP|RAIN|SNOW)([A-Z]+)")


def derive_group(series_ticker, category, subcategory, title):
    """Return (group, group_source, needs_review)."""
    st = series_ticker or ""
    body = st[2:] if st.startswith("KX") else st          # strip KX
    fam = re.match(r"^[A-Z]+", body)
    family = fam.group(0) if fam else body

    if category == "Crypto":
        # asset IS the tag (BTC/ETH/SOL/...) — reliable.
        if subcategory and subcategory != "_none":
            return subcategory, "tag", False
    if category == "Climate and Weather":
        m = WEATHER_RE.match(st)
        if m:
            return m.group(1), "ticker_region", False
    if category == "Sports":
        for lg in sorted(LEAGUES, key=len, reverse=True):
            if body.startswith(lg):
                return lg, "known_league", False
        # unknown sports family -> best effort, flag for review
        return family, "family_prefix", True
    # non-sports/crypto/weather: the series family prefix is a decent group
    return family, "family_prefix", category not in ("Elections", "Politics")


def load_yaml_classes(path):
    """Tiny YAML reader for the flat 2-list schema (no pyyaml dependency)."""
    a, b, cur = [], [], None
    for line in open(path):
        s = line.split("#", 1)[0].rstrip()
        if not s.strip():
            continue
        if s.startswith("class_a_full_l1"):
            cur = a; continue
        if s.startswith("class_b_trades_only"):
            cur = b; continue
        m = re.match(r"\s*-\s*(.+?)\s*$", s)
        if m and cur is not None:
            cur.append(m.group(1))
    return a, b


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", default="work/warehouse")
    ap.add_argument("--config", default="config/market_classes.yaml")
    ap.add_argument("--review", default="config/classification_review.csv")
    args = ap.parse_args(argv[1:])
    import duckdb

    series_pq = os.path.join(args.warehouse, "catalog", "series", "part-00000.parquet")
    con = duckdb.connect()
    rows = con.execute(
        "SELECT ticker, category, tags, title FROM read_parquet('%s')"
        % series_pq.replace("'", "''")).fetchall()

    class_a, class_b = load_yaml_classes(args.config)
    a_set = set(class_a)
    live_cats = sorted({r[1] for r in rows if r[1]})
    missing = [c for c in live_cats if c not in a_set and c not in set(class_b)]
    if missing:
        print("WARNING: live categories not in config (treated as Class B): %s"
              % ", ".join(missing), file=sys.stderr)

    out_rows = []
    for ticker, category, tags, title in rows:
        tags = list(tags) if tags else []
        subcategory = tags[0] if tags else "_none"
        group, gsrc, needs_review = derive_group(ticker, category, subcategory, title)
        klass = "A" if category in a_set else "B"
        out_rows.append({
            "series_ticker": ticker,
            "category": category,
            "subcategory": subcategory,       # pinned (first tag)
            "all_tags": "|".join(tags) if tags else None,
            "group": group,                   # league / region / asset (derived)
            "group_source": gsrc,
            "needs_review": needs_review,
            "record_class": klass,
            "title": title,
        })

    # pinned dim -> parquet
    out_dir = os.path.join(args.warehouse, "catalog", "series_classified")
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False)
    for r in out_rows:
        tmp.write(json.dumps(r) + "\n")
    tmp.close()
    dst = os.path.join(out_dir, "part-00000.parquet").replace("'", "''")
    con.execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited', "
                "union_by_name=true)) TO '%s' (FORMAT PARQUET)" % (tmp.name.replace("'", "''"), dst))
    os.unlink(tmp.name)

    # review CSV (only rows needing review, sorted by category/group)
    os.makedirs(os.path.dirname(args.review), exist_ok=True)
    review = [r for r in out_rows if r["needs_review"]]
    with open(args.review, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category", "subcategory", "group",
                                          "group_source", "series_ticker", "title"])
        w.writeheader()
        for r in sorted(review, key=lambda x: (x["category"] or "", x["group"] or "")):
            w.writerow({k: r[k] for k in w.fieldnames})

    # per-series tags report (ALL series, full tags) for manual review — the
    # subcategory pin is tags[0], so reviewers need to see what was not chosen.
    tags_report = os.path.join(os.path.dirname(args.review), "series_tags_report.csv")
    with open(tags_report, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series_ticker", "category", "subcategory",
                                          "all_tags", "title"])
        w.writeheader()
        for r in sorted(out_rows, key=lambda x: (x["category"] or "", x["series_ticker"])):
            w.writerow({k: r[k] for k in w.fieldnames})

    a_series = sum(1 for r in out_rows if r["record_class"] == "A")
    print("classified %d series | Class A(full-L1)=%d Class B(trades-only)=%d"
          % (len(out_rows), a_series, len(out_rows) - a_series))
    print("review needed for %d series -> %s" % (len(review), args.review))
    print("pinned dim -> %s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
