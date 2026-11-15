import json

import pytest

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
    assert not list(tmp_path.glob("*.tmp"))


def test_partial_ledger_is_quarantined_and_reset(tmp_path, capsys):
    path = tmp_path / "ledger.json"
    path.write_text('{"seq": 42}')
    led = Ledger.load(path)
    assert (led.seq, led.merged) == (0, 0)
    assert list(tmp_path.glob("ledger.json.corrupt-*"))
    assert "corrupt ledger" in capsys.readouterr().err


def test_save_failure_leaves_previous_ledger_intact(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    led = Ledger.load(path)
    led.next_seq()
    led.merged = 1
    led.save()
    before = path.read_text()

    led.seq = 99

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pull_shark.json.dumps", boom)
    with pytest.raises(OSError):
        led.save()
    assert path.read_text() == before


def test_save_creates_nested_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "ledger.json"
    led = Ledger.load(path)
    led.save()
    assert path.exists()


def test_direct_construction_normalises_path(tmp_path):
    led = Ledger(path=str(tmp_path / "ledger.json"))
    led.save()
    assert (tmp_path / "ledger.json").exists()
