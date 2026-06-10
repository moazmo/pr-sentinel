"""Injection / secret-leak defenses (threat model, Threat 2).

Three layers, all cheap:
1. Prompt segregation lives in agents/base.py — PR-controlled text only ever
   appears inside delimited data blocks in the user message. This module
   provides the sanitizer for those blocks.
2. Structured output enforcement lives in models.Finding (unparseable analyst
   output is discarded).
3. This module scrubs the final comment for known secret values and generic
   key-shaped strings before anything is posted — defense in depth: a match
   means an injection got further than it should, so we also log a warning.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"

# Generic key shapes. Deliberately conservative: long, high-entropy-looking
# prefixed tokens only, so normal code identifiers never get redacted.
_GENERIC_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),          # OpenAI-style
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),            # GitHub PAT (classic)
    re.compile(r"gho_[A-Za-z0-9]{20,}"),            # GitHub OAuth token
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),            # GitHub App installation token
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),    # GitHub fine-grained PAT
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),       # Anthropic
    re.compile(r"AKIA[0-9A-Z]{16}"),                # AWS access key id
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),    # Slack
)


def scrub_secrets(text: str, known_secrets: list[str] | None = None) -> str:
    """Redact known secret values and generic key-shaped strings from `text`.

    Called on the final comment (and on log-bound error strings) before they
    leave the process. `known_secrets` carries the actual API key / GitHub
    token values so even a partially mangled echo of them gets caught.
    """
    scrubbed = text
    for secret in known_secrets or []:
        if secret and len(secret) >= 8 and secret in scrubbed:
            logger.warning(
                "SECURITY: a known secret value reached the output path and was redacted."
            )
            scrubbed = scrubbed.replace(secret, REDACTED)
    for pattern in _GENERIC_KEY_PATTERNS:
        scrubbed, n = pattern.subn(REDACTED, scrubbed)
        if n:
            logger.warning(
                "SECURITY: %d key-shaped string(s) matching %s redacted from output.",
                n,
                pattern.pattern,
            )
    return scrubbed


_TAG_BREAKER = re.compile(r"</?\s*(diff|pr_title|pr_description|file)\b[^>]*>", re.IGNORECASE)


def sanitize_for_prompt(text: str) -> str:
    """Neutralize delimiter-escape attempts inside PR-controlled text.

    The prompts wrap untrusted content in <diff>...</diff> style blocks; a
    hostile diff could try to close the block early and smuggle instructions
    outside it. Strip anything that looks like our own delimiters.
    """
    return _TAG_BREAKER.sub("[tag-removed]", text)
