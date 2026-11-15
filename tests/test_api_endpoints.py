from pull_shark import GitHubAPI, GitHubError


def make_api(recorded):
    api = GitHubAPI(token="t", repo="OctoberCoder/pull-shark-lab", sleeper=lambda s: None)

    def fake_request(method, path, payload=None, attempts=3):
        recorded.append((method, path, payload))
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "base_sha"}}
        if "/contents/" in path and method == "GET":
            raise GitHubError(404, "not found")
        if path.endswith("/pulls"):
            return {"number": 7}
        return {"ok": True}

    api.request = fake_request
    return api


def test_default_branch_sha_returns_commit_sha():
    rec = []
    assert make_api(rec).default_branch_sha("main") == "base_sha"
    assert rec[0] == ("GET", "/repos/OctoberCoder/pull-shark-lab/git/ref/heads/main", None)


def test_create_ref_targets_branch_name():
    rec = []
    make_api(rec).create_ref("cycle-1", "base_sha")
    assert rec[0][0] == "POST"
    assert rec[0][1] == "/repos/OctoberCoder/pull-shark-lab/git/refs"
    assert rec[0][2] == {"ref": "refs/heads/cycle-1", "sha": "base_sha"}


def test_put_contents_base64_encodes_and_includes_sha_when_updating():
    rec = []
    make_api(rec).put_contents("activity.log", "cycle-1", "hello", "chore: x", sha="abc")
    payload = rec[0][2]
    assert payload["content"] == "aGVsbG8="
    assert payload["sha"] == "abc"
    assert payload["branch"] == "cycle-1"


def test_put_contents_omits_sha_when_creating():
    rec = []
    make_api(rec).put_contents("activity.log", "cycle-1", "hello", "chore: x", sha=None)
    assert "sha" not in rec[0][2]


def test_open_pr_payload():
    rec = []
    assert make_api(rec).open_pr("cycle-1", "main", "title", "body")["number"] == 7
    assert rec[0][2] == {"title": "title", "body": "body", "head": "cycle-1", "base": "main"}


def test_merge_pr_uses_squash():
    rec = []
    make_api(rec).merge_pr(7)
    assert rec[0][0] == "PUT"
    assert rec[0][1].endswith("/pulls/7/merge")
    assert rec[0][2] == {"merge_method": "squash"}


def test_get_contents_returns_none_on_404():
    assert make_api([]).get_contents("activity.log", "main") is None
