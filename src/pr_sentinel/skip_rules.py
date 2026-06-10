"""Built-in file-skip rules (D10). Always on, no config needed — these protect
the user's API budget from lockfiles, vendored trees, and generated output.
User `ignore:` patterns from config are appended to this list.
"""

from __future__ import annotations

from fnmatch import fnmatch

# Patterns are matched against the full repo-relative path with fnmatch.
# `**/`-style recursion is emulated by also matching the basename where the
# pattern carries no slash.
BUILTIN_SKIP_PATTERNS: tuple[str, ...] = (
    # Lockfiles
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    # Vendored / build output
    "node_modules/*",
    "vendor/*",
    "dist/*",
    "build/*",
    ".next/*",
    "target/*",
    # Minified / maps / assets
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.svg",
    "*.lock",
    # Generated code markers
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.*",
    "*.g.dart",
    "*.snap",
)


def _matches(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    if fnmatch(path, pattern):
        return True
    # Directory patterns like "node_modules/*" must match at any depth.
    if pattern.endswith("/*") and f"/{pattern[:-2]}/" in f"/{path}":
        return True
    # Bare filename patterns match the basename anywhere in the tree.
    if "/" not in pattern and fnmatch(path.rsplit("/", 1)[-1], pattern):
        return True
    return False


def skip_reason(path: str, extra_patterns: list[str] | None = None) -> str | None:
    """Return a human-readable reason if `path` should be skipped, else None."""
    for pattern in BUILTIN_SKIP_PATTERNS:
        if _matches(path, pattern):
            return f"matches built-in skip pattern `{pattern}`"
    for pattern in extra_patterns or []:
        # Users tend to write gitignore-style "dir/**"; normalize to fnmatch.
        normalized = pattern.replace("**", "*")
        if _matches(path, normalized):
            return f"matches ignore pattern `{pattern}`"
    return None
