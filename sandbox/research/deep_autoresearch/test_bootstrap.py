import hashlib
import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("bootstrap.py")
SPEC = importlib.util.spec_from_file_location("deep_bootstrap", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def test_cycle_has_at_most_ten_and_root_event_unit():
    cards = MOD.hypothesis_cards()
    assert 1 <= len(cards) <= 10
    assert len({c["hypothesis_id"] for c in cards}) == len(cards)
    assert all(c["unit_of_inference"] == "root_event" for c in cards)
    assert all(c["split"] == "EXPLORATORY_ONLY" for c in cards)
    assert all("VERDICT_PASS" in c["authority_boundary"] for c in cards)


def test_required_candidate_first_families_are_registered():
    ids = {c["hypothesis_id"] for c in MOD.hypothesis_cards()}
    assert "C1-SPREAD-CAPTURE-01" in ids
    assert "C1-HFOLLOW-RETREAT-01" in ids
    assert "C1-THREEWAY-OVERROUND-01" in ids
    assert "C1-PREMATCH-TTS-01" in ids
    assert "C1-LARGE-FLOW-CONTINUATION-01" in ids
    assert "C1-RFQ-CLOB-01" in ids
    assert "C1-DEPLETION-REFILL-01" in ids
    assert "C1-ANOM-RFQ-SIZE-TAIL-01" in ids
    assert "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01" in ids


def test_pinned_hash_constants_are_full_sha256():
    assert len(MOD.EXPECTED_MISSION_SHA) == 64
    assert len(MOD.EXPECTED_PROMPT_SHA) == 64
    int(MOD.EXPECTED_MISSION_SHA, 16)
    int(MOD.EXPECTED_PROMPT_SHA, 16)


def test_json_writer_is_unicode_and_stable(tmp_path):
    path = tmp_path / "x.json"
    MOD.write_json(path, {"中文": 1, "a": [2]})
    assert json.loads(path.read_text()) == {"中文": 1, "a": [2]}
    first = hashlib.sha256(path.read_bytes()).hexdigest()
    MOD.write_json(path, {"a": [2], "中文": 1})
    assert hashlib.sha256(path.read_bytes()).hexdigest() == first
