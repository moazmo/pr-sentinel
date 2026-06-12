"""GitHub I/O with a mocked API: pagination, sticky upsert, base-branch config."""

import base64
import json

import httpx
import pytest

from pr_sentinel.github_client import COMMENT_MARKER, GitHubClient


class FakeGitHub:
    """Routes httpx requests to canned responses and records writes."""

    def __init__(self):
        self.comments: list[dict] = []
        self.patched: list[tuple[int, str]] = []
        self.posted: list[str] = []
        self.files_pages: list[list[dict]] = [[]]
        self.contents: dict[tuple[str, str], str] = {}
        self.fail_patch = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path.endswith("/files"):
            page = int(params.get("page", 1))
            data = self.files_pages[page - 1] if page <= len(self.files_pages) else []
            return httpx.Response(200, json=data)
        if "/contents/" in path:
            name = path.split("/contents/")[1]
            ref = params.get("ref", "")
            content = self.contents.get((name, ref))
            if content is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            })
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=self.comments)
        if path.endswith("/comments") and request.method == "POST":
            body = json.loads(request.content)["body"]
            self.posted.append(body)
            return httpx.Response(201, json={"id": 999})
        if "/issues/comments/" in path and request.method == "PATCH":
            if self.fail_patch:
                return httpx.Response(404, json={})
            comment_id = int(path.rsplit("/", 1)[1])
            self.patched.append((comment_id, json.loads(request.content)["body"]))
            return httpx.Response(200, json={"id": comment_id})
        return httpx.Response(404, json={})


@pytest.fixture
def fake(monkeypatch):
    fake = FakeGitHub()
    transport = httpx.MockTransport(fake.handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return fake


@pytest.fixture
async def client():
    c = GitHubClient("test-token", "octo/demo")
    yield c
    await c.aclose()  # close the pooled client so it isn't GC'd across loops


class TestListFiles:
    async def test_fields_mapped(self, fake, client):
        fake.files_pages = [[{
            "filename": "app.py", "status": "renamed", "additions": 3, "deletions": 1,
            "patch": "@@ -1 +1 @@", "previous_filename": "old.py",
        }]]
        files = await client.list_pr_files(1)
        assert files[0].path == "app.py"
        assert files[0].previous_path == "old.py"

    async def test_pagination(self, fake, client):
        fake.files_pages = [
            [{"filename": f"f{i}.py", "status": "added"} for i in range(100)],
            [{"filename": "last.py", "status": "added"}],
        ]
        files = await client.list_pr_files(1)
        assert len(files) == 101

    async def test_binary_has_no_patch(self, fake, client):
        fake.files_pages = [[{"filename": "logo.png", "status": "added"}]]
        files = await client.list_pr_files(1)
        assert files[0].patch is None


class TestUpsert:
    async def test_creates_when_no_marker_comment(self, fake, client):
        await client.upsert_comment(1, f"review {COMMENT_MARKER}")
        assert len(fake.posted) == 1 and not fake.patched

    async def test_edits_existing_marker_comment(self, fake, client):
        fake.comments = [
            {"id": 1, "body": "unrelated comment"},
            {"id": 42, "body": f"old review {COMMENT_MARKER}"},
        ]
        await client.upsert_comment(1, f"new review {COMMENT_MARKER}")
        assert fake.patched == [(42, f"new review {COMMENT_MARKER}")]
        assert not fake.posted

    async def test_falls_back_to_post_when_edit_fails(self, fake, client):
        fake.comments = [{"id": 42, "body": COMMENT_MARKER}]
        fake.fail_patch = True
        await client.upsert_comment(1, "body")
        assert len(fake.posted) == 1


class TestBaseBranchConfig:
    async def test_reads_from_given_ref(self, fake, client):
        fake.contents[(".pr-sentinel.yml", "main")] = "min_severity: low"
        content = await client.get_file_from_ref(".pr-sentinel.yml", "main")
        assert content == "min_severity: low"

    async def test_missing_file_returns_none(self, fake, client):
        assert await client.get_file_from_ref(".pr-sentinel.yml", "main") is None
