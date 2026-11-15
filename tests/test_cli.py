import pytest

from pull_shark import build_parser, parse_args, parse_pair


def test_parse_pair_splits_on_last_colon():
    assert parse_pair("QueenCy-coder:nkay.cyvy@gmail.com") == (
        "QueenCy-coder",
        "nkay.cyvy@gmail.com",
    )


def test_parse_pair_rejects_missing_colon():
    with pytest.raises(SystemExit):
        parse_pair("nkay.cyvy@gmail.com")


def test_fast_mode_sets_12s_and_no_cooldown():
    args = parse_args(["--fast"])
    assert args.interval == 12.0
    assert args.cooldown_every == 0


def test_default_mode_is_30s_with_cooldown():
    args = parse_args([])
    assert args.interval == 30.0
    assert args.cooldown_every == 50


def test_default_max_is_320():
    assert build_parser().parse_args([]).max == 320


def test_zero_interval_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--interval", "0"])


def test_negative_interval_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--interval", "-30"])


def test_non_positive_max_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--max", "0"])
