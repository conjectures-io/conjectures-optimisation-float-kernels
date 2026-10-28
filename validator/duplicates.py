"""Copies of an earlier entry: refused before the proof, so timing noise cannot crown one.

The same file, or one with only comments and whitespace changed, times like the original; on a
noisy benchmark a copy can still land under the promotion bar against its own source. Comments are
stripped (nested block and doc comments included, string literals untouched) and whitespace
collapsed; renamed identifiers or reordered code are not caught.
"""

from __future__ import annotations

import hashlib
import re

# In code: a string literal (kept whole), a block comment's start, or a line comment.
_CODE = re.compile(r'"(?:\\.|[^"\\])*"|/-|--[^\n]*')
# Inside a block comment: a nested start or an end; quotes and dashes are just comment text.
_COMMENT = re.compile(r"/-|-/")


def normalized(source: str) -> str:
    # The source without comments, its whitespace collapsed to single spaces.
    kept, pos, depth = [], 0, 0
    while True:
        if depth == 0:
            m = _CODE.search(source, pos)
            if m is None:
                kept.append(source[pos:])
                break
            if m.group().startswith('"'):
                kept.append(source[pos : m.end()])
            else:
                kept.append(source[pos : m.start()])
                depth = 1 if m.group() == "/-" else 0
            pos = m.end()
        else:
            m = _COMMENT.search(source, pos)
            if m is None:
                break  # an unclosed comment runs to the end of the file
            depth += 1 if m.group() == "/-" else -1
            pos = m.end()
    return " ".join("".join(kept).split())


def normalized_digest(content: bytes) -> str:
    return hashlib.sha256(normalized(content.decode("utf-8", "replace")).encode()).hexdigest()
