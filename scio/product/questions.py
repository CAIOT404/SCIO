"""The question channel's formats (ADR 0020, issue #141 part 5): the one place that knows them.

A feature attempt that ends with a design question parks the feature. The question goes to the
feature's issue as a comment that starts with :func:`marker`, and the issue gets
:data:`QUESTION_LABEL`. The owner answers with a comment that starts with
:data:`ANSWER_PREFIX`, on the issue or from the dashboard. Without a repository, the answer is a
file under :data:`ANSWERS_DIR` in the workspace. The build reads answers with
:func:`parse_answer` and :func:`read_answer_file`; the dashboard writes them with
:func:`write_answer_file` or posts an ``answer:`` comment. Nothing else parses these formats.

Pure functions only: no ``gh``, no network. Callers do the I/O.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

QUESTION_LABEL = "needs-answer"
"""The product repository's label on an issue whose feature waits for an answer."""
ANSWER_PREFIX = "answer:"
"""An answer comment starts with this, case-insensitively. SCIO comments with the owner's
credentials, so the prefix, not the author, tells an answer from a question."""
ANSWERS_DIR = Path(".scio") / "answers"
"""Answer files, relative to the workspace (never inside the product's repository)."""
MARKER_RE = re.compile(r"<!-- scio:question feature=(\S+) attempt=(\d+) id=(\S+) -->")


@dataclass(frozen=True)
class Question:
    """One design question an attempt stopped with."""

    feature: str
    attempt: int
    text: str

    @property
    def question_id(self) -> str:
        """Stable across reruns: the same attempt always gets the same id."""
        return question_id(self.feature, self.attempt)


def question_id(feature: str, attempt: int) -> str:
    """``P29-a1`` for feature P29's first attempt."""
    return f"{feature}-a{attempt}"


def marker(q: Question) -> str:
    """The question comment's first line, which :func:`parse_answer` finds it by."""
    return f"<!-- scio:question feature={q.feature} attempt={q.attempt} id={q.question_id} -->"


def format_question(q: Question, ledger_tail: Sequence[str] = ()) -> str:
    """The comment that asks ``q`` on the feature's issue (or the offline issue file)."""
    lines = [marker(q), "", f"**Design question from {q.feature}, attempt {q.attempt}:**", ""]
    lines += [q.text.strip()]
    if ledger_tail:
        lines += ["", "The attempt's last steps:", *(f"- {line}" for line in ledger_tail)]
    lines += [
        "",
        f"Answer with a comment that starts with `{ANSWER_PREFIX}`, or from the SCIO "
        f"dashboard. The build reads it on its next pass and gives {q.feature} a new attempt "
        "with your answer. Other features keep building meanwhile.",
    ]
    return "\n".join(lines)


def parse_answer(comments: Sequence[str], qid: str) -> str | None:
    """The answer to question ``qid``: the first comment after that question's marker comment
    whose text starts with :data:`ANSWER_PREFIX`, without the prefix. Comments are oldest
    first. None while unanswered, and for an empty answer."""
    asked = False
    for body in comments:
        m = MARKER_RE.search(body)
        if m:
            asked = asked or m.group(3) == qid
            continue  # a question comment is never an answer, whichever question it asks
        text = body.lstrip()
        if asked and text[: len(ANSWER_PREFIX)].lower() == ANSWER_PREFIX:
            return text[len(ANSWER_PREFIX) :].strip() or None
    return None


def answer_path(workspace: Path, qid: str) -> Path:
    """Where the answer to ``qid`` lives when the product has no repository."""
    return workspace / ANSWERS_DIR / f"{qid}.md"


def read_answer_file(workspace: Path, qid: str) -> str | None:
    """The answer file's text, stripped; None when missing or empty."""
    path = answer_path(workspace, qid)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_answer_file(workspace: Path, qid: str, text: str) -> Path:
    """Write the owner's answer to ``qid`` (the dashboard's offline path) and return its path."""
    path = answer_path(workspace, qid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8", newline="\n")
    return path
