"""A credential must not reach the screen, however it comes back."""

from __future__ import annotations

import base64
import time

import pytest

from codexswap import backend, redaction

TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"   # synthetic; never a real credential


def percent(text: str) -> str:
    return "".join(f"%{ord(char):02X}" for char in text)


def json_escaped(text: str) -> str:
    return "".join(chr(92) + "u" + format(ord(char), "04x") for char in text)


@pytest.mark.parametrize("name,body", [
    ("literal", TOKEN),
    ("percent-encoded", percent(TOKEN)),
    ("json-escaped", json_escaped(TOKEN)),
    ("split-by-spaces", " ".join(TOKEN[i:i + 4] for i in range(0, len(TOKEN), 4))),
    ("split-by-newlines", "\n".join(TOKEN[i:i + 4] for i in range(0, len(TOKEN), 4))),
    ("case-swapped", TOKEN.swapcase()),
    ("in-a-url", "https://example.test/?session=" + percent(TOKEN)),
    ("nine-char-fragment", TOKEN[:9]),
    ("tail-fragment", TOKEN[-12:]),
])
def test_no_recognisable_form_of_the_token_survives(name, body):
    detail = "server said: " + body

    cleaned = redaction.redact_fragments(detail, TOKEN)

    assert body not in cleaned
    assert "[redacted]" in cleaned


def test_ordinary_text_is_left_alone():
    detail = "Codex app-server exited before answering account/rateLimits/read"

    assert redaction.redact_fragments(detail, TOKEN) == detail


def test_a_short_secret_is_never_used_as_a_window():
    # Eight characters would start blanking ordinary words.
    assert redaction.redact_fragments("the quick brown fox", "abcdefgh") == "the quick brown fox"


def test_base64_of_the_whole_token_is_a_documented_gap():
    encoded = base64.b64encode(TOKEN.encode()).decode()

    # Nothing links those bytes back to a position in the text. Exact replacement of
    # the literal token is what covers the ordinary case; this records the limit.
    assert encoded in redaction.redact_fragments("body=" + encoded, TOKEN)


def test_redact_known_replaces_longest_first():
    text = "a=" + TOKEN + " b=" + TOKEN[:16]

    cleaned = redaction.redact_known(text, [TOKEN[:16], TOKEN, None, ""])

    assert cleaned == "a=[redacted] b=[redacted]"


def test_redaction_stays_fast_on_a_large_body():
    secret = "Ab7dEf2hIj9lMn5p" * 256
    detail = ("unexpected " * 95_000)[:1_048_576]

    started = time.perf_counter()
    redaction.redact_fragments(detail, secret)
    elapsed = time.perf_counter() - started

    # A blow-up guard, not a speed target: the scan is linear in the body length.
    assert elapsed < 20


def test_a_token_cut_by_the_detail_limit_does_not_leak_its_tail():
    padding = "api_key=" + chr(34) + "x" * 4000 + chr(34) + " "
    detail = padding + TOKEN

    # backend redacts the whole body before taking the last 300 characters.
    cleaned = backend._safe_detail(detail, TOKEN)[:300]

    assert TOKEN[:9] not in cleaned
    assert TOKEN[-9:] not in cleaned
