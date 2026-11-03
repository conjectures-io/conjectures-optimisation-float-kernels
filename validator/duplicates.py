"""Copies of an earlier entry: refused before the proof, so timing noise cannot crown one.

The same file, or one with only comments and whitespace changed, times like the original; on a
noisy benchmark a copy can still land under the promotion bar against its own source. Comments are
stripped (nested block and doc comments included, string literals untouched) and whitespace
collapsed; renamed identifiers or reordered code are not caught.
"""

from __future__ import annotations

import hashlib
import re

# Find a string's opening quote without trying to match its entire contents. A failed
# whole-string regex can retry from every escaped quote in malformed input, quadratically.
_CODE = re.compile(r'"|/-|--[^\n]*')
_COMMENTS_ONLY = re.compile(r"/-|--[^\n]*")
_STRING = re.compile(r'\\[\s\S]|"')
# Inside a block comment: a nested start or an end; quotes and dashes are just comment text.
_COMMENT = re.compile(r"/-|-/")


def without_comments(source: str, *, respect_strings: bool = True) -> str:
    """Strip nested comments in one pass, preserving their token-separating whitespace.

    The option policy intentionally treats comment markers inside strings conservatively;
    callers can request that historical behavior without a backtracking comment regex.
    """
    kept, pos, depth = [], 0, 0
    while True:
        if depth == 0:
            m = (_CODE if respect_strings else _COMMENTS_ONLY).search(source, pos)
            if m is None:
                kept.append(source[pos:])
                break
            if m.group() == '"':
                end = m.end()
                while True:
                    token = _STRING.search(source, end)
                    if token is None:
                        end = len(source)  # Keep an unterminated literal; Lean will reject it.
                        break
                    end = token.end()
                    if token.group() == '"':
                        break
                kept.append(source[pos:end])
                pos = end
                continue
            else:
                kept.append(source[pos : m.start()])
                kept.append(" ")
                depth = 1 if m.group() == "/-" else 0
            pos = m.end()
        else:
            m = _COMMENT.search(source, pos)
            if m is None:
                break  # an unclosed comment runs to the end of the file
            depth += 1 if m.group() == "/-" else -1
            pos = m.end()
    return "".join(kept)


def normalized(source: str) -> str:
    return " ".join(without_comments(source).split())


def normalized_digest(content: bytes) -> str:
    return hashlib.sha256(normalized(content.decode("utf-8", "replace")).encode()).hexdigest()
