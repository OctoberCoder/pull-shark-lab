from pull_shark import Pacer, backoff_delays


class FakeSleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def test_next_delay_stays_within_jitter_bounds():
    pacer = Pacer(interval=30.0, jitter=0.2, sleeper=lambda s: None)
    for _ in range(200):
        assert 24.0 <= pacer.next_delay() <= 36.0


def test_next_delay_never_negative():
    pacer = Pacer(interval=30.0, jitter=1.5, sleeper=lambda s: None)
    for _ in range(50):
        assert pacer.next_delay() >= 0.0


def test_zero_interval_yields_zero():
    pacer = Pacer(interval=0.0, jitter=0.0, sleeper=lambda s: None)
    assert pacer.next_delay() == 0.0


def test_cool_down_fires_exactly_every_50_cycles():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=10.0, jitter=0.0, cooldown_every=50, cooldown_secs=300, sleeper=sleeper)
    for _ in range(100):
        pacer.sleep()
    assert sleeper.calls[49] == 300
    assert sleeper.calls[99] == 300
    assert sleeper.calls.count(300) == 2
    others = [c for i, c in enumerate(sleeper.calls) if i not in (49, 99)]
    assert all(c == 10.0 for c in others)


def test_first_cycle_does_not_trigger_cool_down():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=30.0, jitter=0.0, cooldown_every=50, sleeper=sleeper)
    pacer.sleep()
    assert sleeper.calls == [30.0]


def test_fast_mode_skips_cool_down():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=12.0, jitter=0.0, cooldown_every=0, sleeper=sleeper)
    for _ in range(60):
        pacer.sleep()
    assert sleeper.calls.count(300) == 0
    assert set(sleeper.calls) == {12.0}


def test_stalled_loop_does_not_repeat_cool_down():
    sleeper = FakeSleeper()
    pacer = Pacer(interval=10.0, jitter=0.0, cooldown_every=10, cooldown_secs=300, sleeper=sleeper)
    for _ in range(12):
        pacer.sleep()
    assert sleeper.calls.count(300) == 1


def test_backoff_progression_is_capped():
    gen = backoff_delays()
    assert [next(gen) for _ in range(5)] == [60.0, 120.0, 240.0, 240.0, 240.0]
