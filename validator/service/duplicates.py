"""Copies of an accepted Kernel.lean: refused before the gate, so timing noise cannot crown one.

The same file from another hotkey, or with only comments and whitespace changed, times like the
original; on a noisy benchmark one copy in a few still lands under the promotion bar against its
own source. A submission whose normalised source matches an earlier accepted submission in its
slot is rejected at intake. Comments are stripped (nested block and doc comments included, string
literals untouched) and whitespace collapsed; renamed identifiers or reordered code are not caught.
"""

from __future__ import annotations

import hashlib
import re

from db import Store, models

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


def normalized_digest(kernel: bytes) -> str:
    return hashlib.sha256(normalized(kernel.decode("utf-8", "replace")).encode()).hexdigest()


def identical_to(store: Store, sub: models.Submission) -> int | None:
    # The earliest accepted submission in the slot whose normalised Kernel.lean matches this one's.
    mine = store.submissions.kernel(sub.id)
    if mine is None:
        return None
    digest = normalized_digest(mine)
    for other_id, other_digest in store.submissions.accepted_before(sub.slot, sub.id):
        if other_digest == sub.digest:
            return other_id
        other = store.submissions.kernel(other_id)
        if other is not None and normalized_digest(other) == digest:
            return other_id
    return None
