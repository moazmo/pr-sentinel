"""Real-PR benchmark (L4): the risky pure logic is the unified-diff reversal that
turns a bug *fix* into a bug *reintroduction*. The discovery + pipeline run are
live/network and not unit-tested here."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "realpr", Path(__file__).resolve().parent.parent / "evals" / "realpr.py"
)
realpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(realpr)


class TestReversePatch:
    def test_added_and_removed_swap(self):
        # Fix removed the buggy line and added the fixed one; reversing must
        # re-add the buggy line (what a reviewer should then flag).
        fix = "@@ -10,3 +10,3 @@ def f():\n a = 1\n-    return unsafe(x)\n+    return safe(x)\n b = 2\n"
        rev = realpr.reverse_patch(fix)
        assert "+    return unsafe(x)" in rev  # bug reintroduced as an added line
        assert "-    return safe(x)" in rev
        assert " a = 1" in rev and " b = 2" in rev  # context unchanged

    def test_hunk_header_ranges_swap(self):
        fix = "@@ -1,2 +1,3 @@ ctx\n a\n-bug\n+fix1\n+fix2\n"
        rev = realpr.reverse_patch(fix)
        assert "@@ -1,3 +1,2 @@ ctx" in rev
        assert "+bug" in rev
        assert "-fix1" in rev and "-fix2" in rev

    def test_file_header_lines_not_flipped(self):
        # Defensive: +++/--- markers (if present) must not be treated as content.
        p = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n"
        rev = realpr.reverse_patch(p)
        assert "--- a/f" in rev and "+++ b/f" in rev

    def test_reintroduced_lines_are_addressable(self):
        from pr_sentinel.diffmap import added_line_numbers
        rev = realpr.reverse_patch("@@ -5,2 +5,2 @@\n ctx\n-buggy_call()\n+fixed_call()\n")
        # The reintroduced buggy line should be an added line we can anchor to.
        assert len(added_line_numbers(rev)) >= 1


def test_fixed_files_uses_forward_patch():
    # Precision proxy: fixed_files keeps the forward (merged-fix) patch, so its
    # `+` lines are the accepted-correct code a false positive would flag.
    from pr_sentinel.diffmap import added_line_numbers
    entry = {"file": "a.py", "patch": "@@ -1,2 +1,3 @@\n ctx\n-bug\n+fixed1\n+fixed2\n"}
    files = realpr.fixed_files(entry)
    assert files[0].path == "a.py"
    assert files[0].patch == entry["patch"]              # forward, not reversed
    assert len(added_line_numbers(files[0].patch or "")) == 2
