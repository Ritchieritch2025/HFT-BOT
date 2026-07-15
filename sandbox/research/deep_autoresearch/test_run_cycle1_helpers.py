import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_cycle1.py")
SPEC = importlib.util.spec_from_file_location("run_cycle1_helpers", MODULE_PATH)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


def test_manifest_inventory_class_covers_analytic_and_integrity_objects():
    assert mod.manifest_inventory_class(
        "facts/orderbooks_l1/category=Sports/date=2026-07-12/a.parquet"
    ) == "facts/orderbooks_l1"
    assert mod.manifest_inventory_class(
        "facts/orderbooks_full/category=Sports/date=2026-07-12/a.parquet"
    ) == "facts/orderbooks_full_l2"
    assert mod.manifest_inventory_class(
        "dim/snapshots/date=2026-07-12/markets.csv"
    ) == "dim/snapshots/markets.csv"
    assert mod.manifest_inventory_class(
        "raw_rfq/date=2026-07-12/rfq_receipts_07.ndjson"
    ) == "raw_rfq/receipts"
    assert mod.manifest_inventory_class(
        "raw_rfq/date=2026-07-12/rfq_07.ndjson"
    ) == "raw_rfq/messages"
    assert mod.manifest_inventory_class("quality/l2_gaps.json") == "quality/l2_gaps.json"
