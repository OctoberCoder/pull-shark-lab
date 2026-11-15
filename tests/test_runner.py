import pytest

from pull_shark import GitHubError, Ledger, Pacer, Runner


class FakeAPI:
    def __init__(self, fail_merge_times=0):
        self.calls = []
        self.fail_merge_times = fail_merge_times

    def default_branch_sha(self, branch):
        self.calls.append(("sha", branch))
        return "base_sha"

    def create_ref(self, branch, sha):
        self.calls.append(("ref", branch, sha))

    def get_contents(self, path, branch):
        self.calls.append(("get", path))
        return {"content": "aGVsbG8K", "sha": "file_sha"}

    def put_contents(self, path, branch, text, message, sha):
        self.calls.append(("put", text, sha, message))

    def open_pr(self, head, base, title, body=""):
        self.calls.append(("pr", head, title))
        return {"number": 7}

    def merge_pr(self, number):
        self.calls.append(("merge", number))
        if self.fail_merge_times > 0:
            self.fail_merge_times -= 1
            raise GitHubError(405, "not mergeable")
        return {"merged": True}


class NoSleep(Pacer):
    def __init__(self):
        super().__init__(sleeper=lambda s: None)


def make_runner(tmp_path, api, pair=None):
    return Runner(
        api=api,
        ledger=Ledger.load(tmp_path / "ledger.json"),
        pacer=NoSleep(),
        base_branch="main",
        pair=pair,
    )


def test_cycle_performs_four_writes_and_bumps_ledger(tmp_path):
    api = FakeAPI()
    runner = make_runner(tmp_path, api)
    assert runner.run_cycle(1) is True

    kinds = [c[0] for c in api.calls]
    assert kinds == ["sha", "ref", "get", "put", "pr", "merge"]
    assert runner.ledger.merged == 1
    assert (tmp_path / "ledger.json").exists()


def test_cycle_appends_without_dropping_existing_content(tmp_path):
    api = FakeAPI()
    runner = make_runner(tmp_path, api)
    runner.run_cycle(1)
    put = next(c for c in api.calls if c[0] == "put")
    assert put[1].startswith("hello\n")
    assert put[2] == "file_sha"


def test_cycle_creates_file_when_missing(tmp_path):
    api = FakeAPI()
    api.get_contents = lambda path, branch: None
    runner = make_runner(tmp_path, api)
    runner.run_cycle(1)
    put = next(c for c in api.calls if c[0] == "put")
    assert put[2] is None
    assert put[1].startswith("1\t")


def test_pair_mode_adds_coauthor_trailer(tmp_path):
    api = FakeAPI()
    runner = make_runner(tmp_path, api, pair=("QueenCy-coder", "nkay.cyvy@gmail.com"))
    runner.run_cycle(1)
    put = next(c for c in api.calls if c[0] == "put")
    assert "Co-authored-by: QueenCy-coder <nkay.cyvy@gmail.com>" in put[3]


def test_single_405_retries_merge_without_opening_a_second_pr(tmp_path):
    api = FakeAPI(fail_merge_times=1)
    runner = make_runner(tmp_path, api)
    assert runner.run_cycle(1) is True
    assert [c[0] for c in api.calls].count("merge") == 2
    assert [c[0] for c in api.calls].count("pr") == 1


def test_run_stops_after_consecutive_failures(tmp_path):
    class AlwaysFail(FakeAPI):
        def create_ref(self, branch, sha):
            raise GitHubError(422, "Reference already exists")

    api = AlwaysFail()
    runner = make_runner(tmp_path, api)
    merged = runner.run(max_prs=10, max_consecutive_failures=3)
    assert merged == 0
    assert runner.consecutive_failures == 3


def test_run_respects_max_prs(tmp_path):
    api = FakeAPI()
    runner = make_runner(tmp_path, api)
    assert runner.run(max_prs=2) == 2
    assert [c[0] for c in api.calls].count("merge") == 2


def test_run_does_not_pass_progress_to_pacer(tmp_path):
    """cool-down cadence must not depend on a success counter."""
    api = FakeAPI()
    runner = make_runner(tmp_path, api)
    runner.run(max_prs=3)
    assert runner.pacer.cycles == 3


def test_network_failure_aborts_cycle_not_whole_run(tmp_path):
    import urllib.error

    class NetworkFail(FakeAPI):
        def create_ref(self, branch, sha):
            raise urllib.error.URLError("connection reset")

    api = NetworkFail()
    runner = make_runner(tmp_path, api)
    merged = runner.run(max_prs=10, max_consecutive_failures=3)
    assert merged == 0
    assert runner.consecutive_failures == 3


def test_all_merged_entries_persist_across_reload(tmp_path):
    api = FakeAPI()
    runner = make_runner(tmp_path, api)
    runner.run(max_prs=3)
    again = Ledger.load(runner.ledger.path)
    assert again.merged == 3
    assert again.seq == 3
