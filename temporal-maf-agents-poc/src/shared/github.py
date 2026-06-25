"""Idempotent, guarded GitHub write client (activity-side only).

No LLM here. PyGithub is synchronous, so callers invoke create_pr_with_plan via
asyncio.to_thread. PyGithub is imported lazily so mock mode runs without it.
"""

from __future__ import annotations

import re
from typing import Any, Callable

PLAN_PATH_TEMPLATE = "docs/agent-plan-{request_id}.md"
BRANCH_TEMPLATE = "feat/{request_id}"

# Statuses we treat as permanent (no point retrying).
_PERMANENT_STATUS = {401, 403, 404, 422}


class GitHubWriteNotAllowed(Exception):
    """The target repo is not permitted by the owner/allowlist guard."""


class PermanentGitHubError(Exception):
    """A non-retryable GitHub failure (auth, missing repo, validation)."""


def parse_owner_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from an https or ssh GitHub URL."""
    cleaned = repo_url.strip()
    cleaned = re.sub(r"\.git$", "", cleaned)
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+)$", cleaned)
    if not m:
        raise PermanentGitHubError(f"cannot parse owner/repo from {repo_url!r}")
    return m.group(1), m.group(2)


def assert_write_allowed(owner: str, allowed_owner: str | None) -> None:
    """Fail-closed guard: refuse unless the owner matches the allowlist."""
    if not allowed_owner:
        raise GitHubWriteNotAllowed(
            "GITHUB_ALLOWED_OWNER is not set; refusing to write (fail-closed)"
        )
    allowed = {o.strip() for o in allowed_owner.split(",") if o.strip()}
    if owner not in allowed:
        raise GitHubWriteNotAllowed(
            f"owner {owner!r} not in allowed owners {sorted(allowed)}"
        )


def _default_client_factory(token: str | None) -> Any:
    from github import Auth, Github  # type: ignore

    if not token:
        raise PermanentGitHubError("GITHUB_TOKEN is required for live GitHub writes")
    return Github(auth=Auth.Token(token))


def create_pr_with_plan(
    *,
    repo_url: str,
    request_id: str,
    token: str | None,
    allowed_owner: str | None,
    pr_title: str,
    pr_body: str,
    plan_markdown: str,
    commit_message: str,
    client_factory: Callable[[str | None], Any] | None = None,
) -> dict:
    """Ensure branch -> upsert plan file -> ensure PR. Idempotent.

    Raises GitHubWriteNotAllowed / PermanentGitHubError for permanent failures;
    lets transient GithubException (5xx / rate limit) propagate for retry.
    """
    from github import GithubException  # type: ignore

    owner, repo_name = parse_owner_repo(repo_url)
    assert_write_allowed(owner, allowed_owner)

    factory = client_factory or _default_client_factory
    gh_client = factory(token)

    branch = BRANCH_TEMPLATE.format(request_id=request_id)
    path = PLAN_PATH_TEMPLATE.format(request_id=request_id)

    try:
        repo = gh_client.get_repo(f"{owner}/{repo_name}")
        base = repo.default_branch

        # 1. ensure branch
        try:
            repo.get_git_ref(f"heads/{branch}")
        except GithubException as exc:
            if exc.status == 404:
                base_sha = repo.get_git_ref(f"heads/{base}").object.sha
                repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
            else:
                raise

        # 2. upsert plan file on the branch
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.update_file(path, commit_message, plan_markdown, existing.sha, branch=branch)
        except GithubException as exc:
            if exc.status == 404:
                repo.create_file(path, commit_message, plan_markdown, branch=branch)
            else:
                raise

        # 3. ensure PR
        open_pulls = list(repo.get_pulls(state="open", head=f"{owner}:{branch}"))
        if open_pulls:
            pr = open_pulls[0]
            created_or_existed = "existed"
        else:
            pr = repo.create_pull(title=pr_title, body=pr_body, head=branch, base=base)
            created_or_existed = "created"

        return {
            "branch": branch,
            "pr_number": pr.number,
            "pr_url": pr.html_url,
            "plan_file": path,
            "created_or_existed": created_or_existed,
        }

    except GithubException as exc:
        if getattr(exc, "status", None) in _PERMANENT_STATUS:
            raise PermanentGitHubError(f"GitHub {exc.status}: {exc.data}") from exc
        raise  # transient (5xx, secondary rate limit) -> Temporal retry
