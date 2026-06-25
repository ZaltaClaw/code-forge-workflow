from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared import github as gh


def test_parse_owner_repo():
    assert gh.parse_owner_repo("https://github.com/example-org/svc") == ("example-org", "svc")
    assert gh.parse_owner_repo("https://github.com/example-org/svc.git") == ("example-org", "svc")
    assert gh.parse_owner_repo("git@github.com:example-org/svc.git") == ("example-org", "svc")


def test_assert_write_allowed_fail_closed_when_unset():
    with pytest.raises(gh.GitHubWriteNotAllowed):
        gh.assert_write_allowed("example-org", None)


def test_assert_write_allowed_rejects_mismatch():
    with pytest.raises(gh.GitHubWriteNotAllowed):
        gh.assert_write_allowed("someone-else", "example-org")


def test_assert_write_allowed_accepts_match():
    gh.assert_write_allowed("example-org", "example-org")  # no raise


def _fake_repo(*, branch_exists, file_exists, pr_exists):
    repo = MagicMock()
    repo.default_branch = "main"
    base_ref = SimpleNamespace(object=SimpleNamespace(sha="basesha"))

    def get_git_ref(ref):
        if ref == "heads/main":
            return base_ref
        if ref == f"heads/feat/req-1" and branch_exists:
            return SimpleNamespace(object=SimpleNamespace(sha="branchsha"))
        from github import GithubException
        raise GithubException(404, {"message": "Not Found"}, {})

    repo.get_git_ref.side_effect = get_git_ref

    if file_exists:
        repo.get_contents.return_value = SimpleNamespace(sha="filesha")
    else:
        from github import GithubException
        repo.get_contents.side_effect = GithubException(404, {"message": "nf"}, {})

    if pr_exists:
        existing = SimpleNamespace(number=7, html_url="https://github.com/example-org/svc/pull/7")
        repo.get_pulls.return_value = [existing]
    else:
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = SimpleNamespace(
            number=8, html_url="https://github.com/example-org/svc/pull/8"
        )
    return repo


def _factory_for(repo):
    gh_client = MagicMock()
    gh_client.get_repo.return_value = repo
    return lambda token: gh_client


def test_create_pr_fresh(monkeypatch):
    repo = _fake_repo(branch_exists=False, file_exists=False, pr_exists=False)
    out = gh.create_pr_with_plan(
        repo_url="https://github.com/example-org/svc", request_id="req-1",
        token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
        plan_markdown="# plan", commit_message="add plan",
        client_factory=_factory_for(repo),
    )
    assert out["branch"] == "feat/req-1"
    assert out["pr_number"] == 8
    assert out["created_or_existed"] == "created"
    repo.create_git_ref.assert_called_once()  # branch created from base
    repo.create_file.assert_called_once()         # plan file created (not updated)
    repo.create_pull.assert_called_once()         # PR opened


def test_create_pr_idempotent_when_everything_exists(monkeypatch):
    repo = _fake_repo(branch_exists=True, file_exists=True, pr_exists=True)
    out = gh.create_pr_with_plan(
        repo_url="https://github.com/example-org/svc", request_id="req-1",
        token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
        plan_markdown="# plan", commit_message="add plan",
        client_factory=_factory_for(repo),
    )
    assert out["pr_number"] == 7
    assert out["created_or_existed"] == "existed"
    repo.create_git_ref.assert_not_called()      # branch reused
    repo.update_file.assert_called_once()         # file updated, not created
    repo.create_pull.assert_not_called()          # PR reused


def test_create_pr_guard_blocks_disallowed_owner():
    with pytest.raises(gh.GitHubWriteNotAllowed):
        gh.create_pr_with_plan(
            repo_url="https://github.com/someone-else/svc", request_id="req-1",
            token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
            plan_markdown="x", commit_message="m", client_factory=lambda token: MagicMock(),
        )


def test_permanent_error_on_404_repo():
    from github import GithubException
    gh_client = MagicMock()
    gh_client.get_repo.side_effect = GithubException(404, {"message": "nf"}, {})
    with pytest.raises(gh.PermanentGitHubError):
        gh.create_pr_with_plan(
            repo_url="https://github.com/example-org/svc", request_id="req-1",
            token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
            plan_markdown="x", commit_message="m", client_factory=lambda token: gh_client,
        )


def test_create_pr_partial_retry_branch_exists_file_absent():
    # Simulates a Temporal retry after the branch was created but the commit/PR
    # did not happen: must NOT recreate the branch, must create the file + PR.
    repo = _fake_repo(branch_exists=True, file_exists=False, pr_exists=False)
    out = gh.create_pr_with_plan(
        repo_url="https://github.com/example-org/svc", request_id="req-1",
        token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
        plan_markdown="# plan", commit_message="add plan",
        client_factory=_factory_for(repo),
    )
    assert out["created_or_existed"] == "created"
    assert out["pr_number"] == 8
    repo.create_git_ref.assert_not_called()       # branch reused, not recreated
    repo.create_file.assert_called_once()          # file created this attempt
    repo.create_pull.assert_called_once()          # PR opened this attempt


def test_rate_limit_propagates_as_transient():
    from github import RateLimitExceededException
    gh_client = MagicMock()
    gh_client.get_repo.side_effect = RateLimitExceededException(403, {"message": "rate limited"}, {})
    with pytest.raises(RateLimitExceededException):
        gh.create_pr_with_plan(
            repo_url="https://github.com/example-org/svc", request_id="req-1",
            token="t", allowed_owner="example-org", pr_title="T", pr_body="B",
            plan_markdown="x", commit_message="m", client_factory=lambda token: gh_client,
        )
