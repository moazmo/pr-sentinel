"""Comment-command parsing and authorization (V2 B2)."""

from pr_sentinel.main import _command_from_event, parse_command


class TestParseCommand:
    def test_review(self):
        assert parse_command("@pr-sentinel review") == ("review", "")

    def test_describe(self):
        assert parse_command("@pr-sentinel describe") == ("describe", "")

    def test_ask_with_question(self):
        assert parse_command("@pr-sentinel ask why is this O(n^2)?") == (
            "ask", "why is this O(n^2)?"
        )

    def test_ask_without_question_rejected(self):
        assert parse_command("@pr-sentinel ask") is None

    def test_unknown_command_rejected(self):
        assert parse_command("@pr-sentinel deploy") is None

    def test_non_command_ignored(self):
        assert parse_command("looks good to me!") is None

    def test_case_insensitive_prefix(self):
        assert parse_command("@PR-Sentinel review") == ("review", "")


class TestCommandFromEvent:
    def _event(self, body="@pr-sentinel review", assoc="OWNER", is_pr=True, action="created"):
        issue = {"number": 5}
        if is_pr:
            issue["pull_request"] = {"url": "..."}
        return {
            "action": action,
            "issue": issue,
            "comment": {"body": body, "author_association": assoc},
        }

    def test_trusted_owner_command_accepted(self):
        assert _command_from_event(self._event()) == ("review", "", 5)

    def test_collaborator_accepted(self):
        assert _command_from_event(self._event(assoc="COLLABORATOR")) == ("review", "", 5)

    def test_untrusted_author_rejected(self):
        assert _command_from_event(self._event(assoc="NONE")) is None
        assert _command_from_event(self._event(assoc="CONTRIBUTOR")) is None

    def test_comment_on_issue_not_pr_rejected(self):
        assert _command_from_event(self._event(is_pr=False)) is None

    def test_non_command_comment_ignored(self):
        assert _command_from_event(self._event(body="nice work")) is None

    def test_edited_comment_ignored(self):
        assert _command_from_event(self._event(action="edited")) is None

    def test_ask_carries_question(self):
        ev = self._event(body="@pr-sentinel ask what does foo do?")
        assert _command_from_event(ev) == ("ask", "what does foo do?", 5)
