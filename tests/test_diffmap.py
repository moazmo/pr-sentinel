"""Diff geometry (V2 A1): parsing, numbered rendering, line maps, extension."""

from pr_sentinel.diffmap import (
    added_line_numbers,
    extend_patch,
    line_text_map,
    parse_patch,
    render_numbered,
)

PATCH = (
    "@@ -10,6 +10,9 @@ def get_user(uid):\n"
    " conn = get_connection()\n"
    "-    return conn.execute(old)\n"
    "+    q = f\"SELECT * FROM users WHERE id = {uid}\"\n"
    "+    return conn.execute(q).fetchone()\n"
    " # trailing context\n"
)


class TestParse:
    def test_new_line_numbers_follow_header(self):
        hunks = parse_patch(PATCH)
        assert len(hunks) == 1
        h = hunks[0]
        assert h.new_start == 10
        added = [ln for ln in h.lines if ln.tag == "+"]
        # context line 10, removed (no new no), then +11, +12
        assert added[0].new_lineno == 11
        assert added[1].new_lineno == 12

    def test_removed_lines_have_no_new_number(self):
        hunks = parse_patch(PATCH)
        removed = [ln for ln in hunks[0].lines if ln.tag == "-"]
        assert removed and all(ln.new_lineno is None for ln in removed)

    def test_malformed_patch_does_not_raise(self):
        assert parse_patch("not a diff at all") == []


class TestRenderNumbered:
    def test_every_kept_line_prefixed_with_number(self):
        out = render_numbered(PATCH)
        assert "   11 +" in out and "   12 +" in out
        assert "   10  " in out  # context line keeps its number
        assert "      -" in out  # removed line unnumbered

    def test_empty_patch_renders_empty(self):
        assert render_numbered("") == ""


class TestLineMap:
    def test_maps_added_and_context_not_removed(self):
        m = line_text_map(PATCH)
        assert m[11].strip().startswith("q = f")
        assert m[10] == "conn = get_connection()"
        assert 13 in m  # trailing context

    def test_added_line_numbers_only_plus(self):
        added = added_line_numbers(PATCH)
        assert added == {11, 12}


class TestExtendPatch:
    def test_adds_surrounding_context(self):
        patch = "@@ -5,1 +5,2 @@\n line5\n+line5b\n"
        # content must match the patch's context/added lines for extension to apply
        src = ["line1", "line2", "line3", "line4", "line5", "line5b"] + \
              [f"x{i}" for i in range(7, 21)]
        extended = extend_patch(patch, "\n".join(src), context_lines=3)
        assert "line2" in extended  # pulled in as leading context
        assert "x7" in extended     # pulled in as trailing context

    def test_mismatched_content_returns_original(self):
        patch = "@@ -5,1 +5,2 @@\n line5\n+line5b\n"
        extended = extend_patch(patch, "totally different content", context_lines=3)
        assert extended == patch

    def test_zero_context_is_noop(self):
        assert extend_patch(PATCH, "whatever", 0) == PATCH
