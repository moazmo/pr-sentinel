"""GitHub REST I/O (D2, D8).

- Diff acquisition uses ONLY the paginated `List pull request files` endpoint.
  The whole-diff media type hard-fails (HTTP 406) past 3,000 lines / 300
  files; the files endpoint paginates and its per-file `patch` feeds the
  per-file review strategy directly.
- The review comment is sticky: located via a hidden HTML marker and edited
  in place on every push instead of stacking new comments.
- `.pr-sentinel.yml` is fetched from the BASE branch ref, never the PR head,
  so a hostile PR cannot rewrite the config that reviews it.
"""

from __future__ import annotations

import base64
import logging

import httpx

from .models import ChangedFile, PRMetadata

logger = logging.getLogger(__name__)

COMMENT_MARKER = "<!-- pr-sentinel-marker -->"
DESCRIBE_MARKER_START = "<!-- pr-sentinel-describe-start -->"
DESCRIBE_MARKER_END = "<!-- pr-sentinel-describe-end -->"
# GitHub rejects issue comments longer than this.
MAX_COMMENT_CHARS = 65_536


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com") -> None:
        self._repo = repo
        self._api_url = api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method, f"{self._api_url}{path}", headers=self._headers, **kwargs
            )
        if response.status_code >= 400:
            # Never echo response bodies blindly — keep error surface small.
            raise GitHubError(f"GitHub API {method} {path} -> HTTP {response.status_code}")
        return response

    async def list_pr_files(self, pr_number: int, max_pages: int = 10) -> list[ChangedFile]:
        """All changed files, paginated 100 at a time.

        `patch` is None for binaries and for files whose individual diff is too
        large for the API to inline — both are surfaced as skipped later.
        """
        files: list[ChangedFile] = []
        for page in range(1, max_pages + 1):
            response = await self._request(
                "GET",
                f"/repos/{self._repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            for item in batch:
                files.append(
                    ChangedFile(
                        path=item["filename"],
                        status=item.get("status", "modified"),
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                        patch=item.get("patch"),
                        previous_path=item.get("previous_filename"),
                    )
                )
            if len(batch) < 100:
                break
        return files

    async def get_file_from_ref(self, path: str, ref: str) -> str | None:
        """Fetch a file's content at a specific ref (used for base-branch config).
        Returns None when the file doesn't exist."""
        try:
            response = await self._request(
                "GET",
                f"/repos/{self._repo}/contents/{path}",
                params={"ref": ref},
            )
        except GitHubError:
            return None
        data = response.json()
        if isinstance(data, dict) and data.get("encoding") == "base64":
            try:
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except (KeyError, ValueError):
                return None
        return None

    async def upsert_comment(self, pr_number: int, body: str) -> None:
        """Edit the existing PR Sentinel comment if present, else create one.

        If the edit fails (comment deleted between list and patch, etc.),
        fall back to posting a new comment — a duplicate beats silence.
        """
        if len(body) > MAX_COMMENT_CHARS:
            # The formatter already enforces this; belt and suspenders.
            body = body[: MAX_COMMENT_CHARS - 80] + "\n\n*(truncated — see Action logs)*"

        existing_id = await self._find_marker_comment(pr_number)
        if existing_id is not None:
            try:
                await self._request(
                    "PATCH",
                    f"/repos/{self._repo}/issues/comments/{existing_id}",
                    json={"body": body},
                )
                return
            except GitHubError:
                logger.warning("Editing existing comment failed; posting a new one.")
        await self._request(
            "POST",
            f"/repos/{self._repo}/issues/{pr_number}/comments",
            json={"body": body},
        )

    async def get_pr(self, pr_number: int) -> dict:
        """Fetch PR metadata — used by comment-triggered commands, where the
        issue_comment event payload carries no PR details."""
        response = await self._request("GET", f"/repos/{self._repo}/pulls/{pr_number}")
        return response.json()

    async def post_comment(self, pr_number: int, body: str) -> None:
        """A plain (non-sticky) comment — used for @pr-sentinel ask replies."""
        if len(body) > MAX_COMMENT_CHARS:
            body = body[: MAX_COMMENT_CHARS - 80] + "\n\n*(truncated)*"
        await self._request(
            "POST",
            f"/repos/{self._repo}/issues/{pr_number}/comments",
            json={"body": body},
        )

    async def create_inline_review(
        self, pr_number: int, commit_sha: str, comments: list[dict]
    ) -> bool:
        """Post one PR review whose comments anchor to diff lines (V2 B1).

        Each comment: {"path": ..., "line": <new-file line>, "body": ...}.
        Returns False on any failure — the caller falls back to keeping those
        findings in the summary comment (fail-open, like everything else).
        """
        if not comments:
            return True
        payload = {
            "commit_id": commit_sha,
            "event": "COMMENT",
            "body": "",
            "comments": [
                {"path": c["path"], "line": int(c["line"]), "side": "RIGHT",
                 "body": c["body"]}
                for c in comments
            ],
        }
        try:
            await self._request(
                "POST", f"/repos/{self._repo}/pulls/{pr_number}/reviews", json=payload
            )
            return True
        except GitHubError as exc:
            logger.warning("Inline review failed (%s); findings stay in the summary.", exc)
            return False

    async def update_pr_description(self, pr_number: int, generated: str) -> bool:
        """Write the generated description into the PR body BETWEEN markers,
        never touching anything the author wrote outside them (V2 B4)."""
        try:
            pr = await self.get_pr(pr_number)
            body = pr.get("body") or ""
            block = f"{DESCRIBE_MARKER_START}\n{generated}\n{DESCRIBE_MARKER_END}"
            if DESCRIBE_MARKER_START in body and DESCRIBE_MARKER_END in body:
                start = body.index(DESCRIBE_MARKER_START)
                end = body.index(DESCRIBE_MARKER_END) + len(DESCRIBE_MARKER_END)
                new_body = body[:start] + block + body[end:]
            else:
                new_body = (body + "\n\n" if body.strip() else "") + block
            await self._request(
                "PATCH", f"/repos/{self._repo}/pulls/{pr_number}",
                json={"body": new_body[:60_000]},
            )
            return True
        except GitHubError as exc:
            logger.warning("PR description update failed: %s", exc)
            return False

    async def _find_marker_comment(self, pr_number: int, max_pages: int = 5) -> int | None:
        for page in range(1, max_pages + 1):
            response = await self._request(
                "GET",
                f"/repos/{self._repo}/issues/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            for comment in batch:
                if COMMENT_MARKER in (comment.get("body") or ""):
                    return int(comment["id"])
            if len(batch) < 100:
                return None
        return None


def pr_metadata_from_event(event: dict, repo: str) -> PRMetadata:
    """Build PRMetadata from the GitHub Actions `pull_request` event payload."""
    pr = event.get("pull_request") or {}
    return PRMetadata(
        repo=repo,
        number=int(pr.get("number") or event.get("number") or 0),
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
        base_sha=str((pr.get("base") or {}).get("sha") or ""),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        base_ref=str((pr.get("base") or {}).get("ref") or ""),
        author=str((pr.get("user") or {}).get("login") or ""),
    )
