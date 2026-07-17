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
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publication_generation as pg  # noqa: E402


CATALOG_MEMBERS = {
    "series/part-00000.parquet",
    "events/part-00000.parquet",
    "markets/part-00000.parquet",
    "settlements/part-00000.parquet",
    "series_classified/part-00000.parquet",
}

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


def _catalog_paths(warehouse):
    root = os.path.join(warehouse, "catalog")
    if os.path.lexists(root) and os.path.islink(root):
        raise pg.GenerationError("catalog root is a symlink")
    paths = set()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in dirs:
            if os.path.islink(os.path.join(base, name)):
                raise pg.GenerationError("catalog directory is a symlink")
        for name in sorted(files):
            full = os.path.join(base, name)
            if os.path.islink(full):
                raise pg.GenerationError("catalog file is a symlink")
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel not in CATALOG_MEMBERS:
                raise pg.GenerationError("unexpected catalog member %s" % rel)
            paths.add("catalog/" + rel)
    return paths


def _atomic_csv(path, fieldnames, rows):
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.lexists(parent) and os.path.islink(parent):
        raise pg.GenerationError("CSV output directory is a symlink")
    os.makedirs(parent, exist_ok=True)
    fd, pending = tempfile.mkstemp(prefix=".pending-classification-",
                                   dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row[key] for key in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        pending = None
    finally:
        if pending and os.path.exists(pending):
            os.unlink(pending)


def _base_unchanged(warehouse, base, state):
    current = _catalog_paths(warehouse)
    if state == "PRODUCER_MANIFEST_VERIFIED":
        now = pg.load_manifest(
            warehouse, "catalog", expected_paths=current,
            verify_files=False, allow_missing=False)
    else:
        now, _ = pg.verified_or_synthesized_manifest(
            warehouse, "catalog", current)
    if now["generation_id"] != base["generation_id"]:
        raise pg.GenerationError(
            "catalog changed while classification was staged")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", default="work/warehouse")
    ap.add_argument("--config", default="config/market_classes.yaml")
    ap.add_argument("--review", default="config/classification_review.csv")
    args = ap.parse_args(argv[1:])
    import duckdb

    warehouse = os.path.abspath(args.warehouse)
    series_pq = os.path.join(
        warehouse, "catalog", "series", "part-00000.parquet")
    stage_parent = os.path.join(warehouse, ".publication-stage")
    if os.path.lexists(stage_parent) and os.path.islink(stage_parent):
        raise pg.GenerationError("publication stage is a symlink")
    os.makedirs(stage_parent, mode=0o700, exist_ok=True)
    stage_root = tempfile.mkdtemp(prefix="classification-", dir=stage_parent)

    try:
        # Classification is derived and staged while the source catalog
        # generation is shared-locked.  Only final group replacement is
        # exclusive.
        with pg.generation_locks(
                warehouse, {"catalog": "shared"}, timeout=30.0):
            current_paths = _catalog_paths(warehouse)
            base, base_state = pg.verified_or_synthesized_manifest(
                warehouse, "catalog", current_paths)
            con = duckdb.connect()
            try:
                rows = con.execute(
                    "SELECT ticker, category, tags, title "
                    "FROM read_parquet('%s')" %
                    series_pq.replace("'", "''")).fetchall()
            finally:
                con.close()

            class_a, class_b = load_yaml_classes(args.config)
            a_set = set(class_a)
            live_cats = sorted({row[1] for row in rows if row[1]})
            missing = [category for category in live_cats
                       if category not in a_set
                       and category not in set(class_b)]
            if missing:
                print("WARNING: live categories not in config "
                      "(treated as Class B): %s" % ", ".join(missing),
                      file=sys.stderr)

            out_rows = []
            for ticker, category, tags, title in rows:
                tags = list(tags) if tags else []
                subcategory = tags[0] if tags else "_none"
                group, group_source, needs_review = derive_group(
                    ticker, category, subcategory, title)
                out_rows.append({
                    "series_ticker": ticker,
                    "category": category,
                    "subcategory": subcategory,
                    "all_tags": "|".join(tags) if tags else None,
                    "group": group,
                    "group_source": group_source,
                    "needs_review": needs_review,
                    "record_class": "A" if category in a_set else "B",
                    "title": title,
                })
            if not out_rows:
                raise pg.GenerationError("classification source is empty")

            out_dir = os.path.join(
                stage_root, "catalog", "series_classified")
            os.makedirs(out_dir, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                "w", suffix=".ndjson", delete=False, dir=stage_root)
            try:
                for row in out_rows:
                    tmp.write(json.dumps(row) + "\n")
                tmp.close()
                dst = os.path.join(out_dir, "part-00000.parquet")
                con = duckdb.connect()
                try:
                    con.execute(
                        "COPY (SELECT * FROM read_json_auto('%s', "
                        "format='newline_delimited', union_by_name=true)) "
                        "TO '%s' (FORMAT PARQUET)" %
                        (tmp.name.replace("'", "''"),
                         dst.replace("'", "''")))
                finally:
                    con.close()
            finally:
                if os.path.exists(tmp.name):
                    os.unlink(tmp.name)

            # Pin every unchanged member into a complete staged catalog.
            classified_rel = (
                "catalog/series_classified/part-00000.parquet")
            for rel in sorted(current_paths - {classified_rel}):
                src = os.path.join(warehouse, *rel.split("/"))
                dst = os.path.join(stage_root, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.link(src, dst)
            staged_paths = sorted(current_paths | {classified_rel})
            classified_file = pg.attest_files(
                stage_root, [classified_rel])[0]
            prior_files = {
                row["relative_path"]: row for row in base["files"]}
            generation_files = [
                (classified_file if rel == classified_rel
                 else prior_files[rel])
                for rel in staged_paths]
            manifest = pg.build_manifest(
                "catalog", generation_files)

        with pg.generation_locks(
                warehouse, {"catalog": "exclusive"}, timeout=30.0):
            _base_unchanged(warehouse, base, base_state)
            pg.publish_transaction(
                warehouse,
                {rel: os.path.join(stage_root, *rel.split("/"))
                 for rel in staged_paths},
                manifest)

        # Review files are outside the canonical generation, but each is still
        # atomically replaced so readers never see a half-written CSV.
        review = [row for row in out_rows if row["needs_review"]]
        _atomic_csv(
            args.review,
            ["category", "subcategory", "group", "group_source",
             "series_ticker", "title"],
            sorted(review, key=lambda row: (
                row["category"] or "", row["group"] or "")))
        tags_report = os.path.join(
            os.path.dirname(os.path.abspath(args.review)),
            "series_tags_report.csv")
        _atomic_csv(
            tags_report,
            ["series_ticker", "category", "subcategory", "all_tags", "title"],
            sorted(out_rows, key=lambda row: (
                row["category"] or "", row["series_ticker"])))

        a_series = sum(1 for row in out_rows
                       if row["record_class"] == "A")
        print("classified %d series | Class A(full-L1)=%d "
              "Class B(trades-only)=%d" %
              (len(out_rows), a_series, len(out_rows) - a_series))
        print("review needed for %d series -> %s" %
              (len(review), args.review))
        print("pinned dim -> %s [catalog generation %s]" % (
            os.path.join(warehouse, "catalog", "series_classified"),
            manifest["generation_id"][:16]))
        return 0
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
