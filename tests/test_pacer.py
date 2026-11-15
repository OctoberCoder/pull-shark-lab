from pull_shark import Pacer


class FakeSleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def test_delay_stays_within_jitter_bounds():
    pacer = Pacer(interval=30.0, jitter=0.2, sleeper=lambda s: None)
    for _ in range(200):
        assert 24.0 <= pacer.delay() <= 36.0


def test_default_mode_cools_down_every_50_cycles():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=30.0, jitter=0.0, cooldown_every=50, cooldown_secs=300, sleeper=sleeper)
    pacer.sleep(cycles_done=50)
    assert sleeper.calls == [300]


def test_fast_mode_skips_cool_down():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=12.0, jitter=0.0, cooldown_every=0, sleeper=sleeper)
    pacer.sleep(cycles_done=50)
    assert sleeper.calls == [12.0]


def test_first_cycle_does_not_trigger_cool_down():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=30.0, jitter=0.0, cooldown_every=50, sleeper=sleeper)
    pacer.sleep(cycles_done=0)
    assert sleeper.calls == [30.0]


def test_backoff_progression_is_capped():
    pacer = Pacer(interval=30.0, sleeper=lambda s: None)
    gen = pacer.backoff_delays()
    assert [next(gen) for _ in range(5)] == [60, 120, 240, 240, 240]
