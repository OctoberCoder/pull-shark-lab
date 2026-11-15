import json

from pull_shark import Ledger


def test_load_missing_file_starts_at_zero(tmp_path):
    led = Ledger.load(tmp_path / "ledger.json")
    assert led.seq == 0
    assert led.merged == 0


def test_round_trip_persists_counters(tmp_path):
    path = tmp_path / "ledger.json"
    led = Ledger.load(path)
    led.next_seq()
    led.merged += 1
    led.save()

    again = Ledger.load(path)
    assert again.seq == 1
    assert again.merged == 1
    assert again.updated_at != ""


def test_next_seq_is_monotonic(tmp_path):
    led = Ledger.load(tmp_path / "ledger.json")
    assert [led.next_seq() for _ in range(3)] == [1, 2, 3]


def test_corrupt_file_recovers_to_zero(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json")
    led = Ledger.load(path)
    assert led.seq == 0


def test_save_is_atomic_and_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "ledger.json"
    led = Ledger.load(path)
    led.save()
    assert json.loads(path.read_text())["seq"] == 0
    assert not (tmp_path / "ledger.tmp").exists()
