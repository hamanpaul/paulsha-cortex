"""Shared GitHub rate-limit vs. authentication-failure classification (#370).

Three independent call sites need to tell a GitHub *rate limit* (retryable,
resolves itself once the window resets) apart from a real *authentication*
failure (not retryable, needs operator action): the Monitor's
``GitHubWorkProvider.scan()`` (``monitor/providers.py``), the coordinator's
canonical work-authority classification (``coordinator/claim.py``), and the
``cortex doctor`` ``gh-auth`` probe (``doctor.py``). Before #370 each of
these either didn't check rate-limit wording at all, or checked "auth"
*before* "rate limit" — and GitHub's own secondary/abuse-detection rate
limit messages often mention "OAuth" or invite you to "authenticate" again,
so an auth-first check misfiles a retryable rate limit as a dead credential.

Rate limit is therefore always checked first here: a message that matches
both signals is a rate limit, never an auth failure.
"""

from __future__ import annotations

import re

# Primary quota, secondary/abuse-detection limits, and the standard rate
# limit HTTP signals/headers. Deliberately broad — a false positive here
# just means a real auth failure is (correctly, still safely) treated as
# "retryable rate limit" for one probe cycle, whereas a false negative
# reintroduces the #370 misclassification.
_RATE_LIMIT_PATTERN = re.compile(
    r"""
    rate\ limit           # "rate limit exceeded", "secondary rate limit"
    | abuse\ detection     # "You have triggered an abuse detection mechanism"
    | x-ratelimit          # X-RateLimit-Remaining / -Reset headers echoed into stderr
    | retry-after          # Retry-After header echoed into stderr
    | \b403\b              # GitHub's rate-limit HTTP status
    | \b429\b              # Too Many Requests
    """,
    re.IGNORECASE | re.VERBOSE,
)

# #487: the ``oauth`` alternative used to be unbounded, so it matched *inside*
# ordinary identifiers — a Claude builder's normal init skill list contains
# ``doc-coauthoring``, whose ``coauthoring`` substring contains ``oauth``. That
# turned an unrelated tool failure into a non-retryable ``auth`` classification
# and blocked the correct recovery path. The signal is now bounded to a
# standalone token: ``oauth token`` / ``OAuth-2.0`` still match, ``coauthoring``
# does not. ``authenticat`` stays unbounded on purpose — it is a prefix of
# ``authenticate``/``authentication``/``authenticating`` and has no benign
# substring host of the same kind.
_AUTH_PATTERN = re.compile(
    r"bad credentials|\b401\b|authenticat|\boauth\b|token.*invalid|invalid.*token",
    re.IGNORECASE,
)


def is_rate_limit_signal(message: str | None) -> bool:
    """True when *message* (gh CLI stderr, API error body, ...) reads as a GitHub rate limit."""
    return bool(message) and _RATE_LIMIT_PATTERN.search(message) is not None


def is_auth_signal(message: str | None) -> bool:
    """True when *message* reads as a credential/authentication failure.

    Callers must check :func:`is_rate_limit_signal` first — see module
    docstring — since rate-limit messages can also match this pattern.
    """
    return bool(message) and _AUTH_PATTERN.search(message) is not None
