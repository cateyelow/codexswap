"""One home for the rule that a credential must never reach the user's screen.

Both the supported app-server path and the undocumented backend quote text they did
not write: a subprocess's stderr, an HTTP error body. Either can echo the token that
was sent to it. Exact replacement handles the ordinary case; this module handles the
cases where the token comes back looking slightly different.
"""

from __future__ import annotations

# CONTRACT: Preserve the documented typing annotations for Python 3.9 callers.
# ruff: noqa: UP006, UP007, UP035
from typing import Iterable, List, Optional, Set, Tuple

# Nine characters is short enough to catch a truncated token and long enough that
# ordinary English never collides with one.
WINDOW = 9
_HEX = "0123456789abcdefABCDEF"


def _decoded(text: str) -> Tuple[str, List[Tuple[int, int]]]:
    """A whitespace-free, percent- and \\u-decoded view, plus each char's origin span.

    Returned spans are half-open `(start, stop)` indices into `text`, so a match in
    the decoded view can be blanked out of the original exactly.
    """
    chars: List[str] = []
    spans: List[Tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if (char == "%" and index + 2 < length
                and text[index + 1] in _HEX and text[index + 2] in _HEX):
            chars.append(chr(int(text[index + 1:index + 3], 16)))
            spans.append((index, index + 3))
            index += 3
            continue
        if (char == "\\" and index + 5 < length and text[index + 1] in "uU"
                and all(digit in _HEX for digit in text[index + 2:index + 6])):
            chars.append(chr(int(text[index + 2:index + 6], 16)))
            spans.append((index, index + 6))
            index += 6
            continue
        chars.append(char)
        spans.append((index, index + 1))
        index += 1
    return "".join(chars), spans


def _windows(secret: str) -> Set[str]:
    folded = secret.casefold()
    return {folded[i:i + WINDOW] for i in range(len(folded) - WINDOW + 1)}


def redact_fragments(text: str, secret: str) -> str:
    """Blank every span of `text` that repeats any window of `secret`.

    Catches a token that arrives truncated, split across whitespace, percent-encoded
    in a URL, escaped into JSON, or in a different case. It does not catch a token
    that was re-encoded whole, base64 for instance, because nothing links the bytes
    back to a position in the text. Exact replacement of known secrets runs first and
    covers the ordinary case.
    """
    if len(secret) < WINDOW or len(text) < WINDOW:
        return text
    view, spans = _decoded(text)
    if len(view) < WINDOW:
        return text
    wanted = _windows(secret)
    folded = view.casefold()
    hidden = bytearray(len(text))
    found = False
    for start in range(len(view) - WINDOW + 1):
        if folded[start:start + WINDOW] not in wanted:
            continue
        found = True
        for position in range(start, start + WINDOW):
            begin, stop = spans[position]
            for index in range(begin, stop):
                hidden[index] = 1
    if not found:
        return text
    out: List[str] = []
    index = 0
    while index < len(text):
        if hidden[index]:
            out.append("[redacted]")
            while index < len(text) and hidden[index]:
                index += 1
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def redact_known(text: str, secrets: Iterable[Optional[str]]) -> str:
    """Replace each secret wherever it appears literally, longest first."""
    values = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    for secret in values:
        text = text.replace(secret, "[redacted]")
    return text
