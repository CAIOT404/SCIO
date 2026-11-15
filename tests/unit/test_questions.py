"""The question channel's formats (ADR 0020): a question comment, its answer, the answer file."""

from __future__ import annotations

from pathlib import Path

from scio.product.questions import (
    ANSWER_PREFIX,
    Question,
    format_question,
    parse_answer,
    read_answer_file,
    write_answer_file,
)

Q = Question(feature="P29", attempt=1, text="Leading dot, trailing dot, or both?")


def test_a_question_comment_is_answered_by_the_next_answer_comment() -> None:
    asked = format_question(Q, ledger_tail=["coder: design decision needed: ..."])
    assert asked.splitlines()[0] == "<!-- scio:question feature=P29 attempt=1 id=P29-a1 -->"
    assert ANSWER_PREFIX in asked and "Leading dot" in asked
    comments = ["an earlier note", asked, "thinking...", "Answer: both, as on a TI", "answer: x"]
    assert parse_answer(comments, Q.question_id) == "both, as on a TI"  # first one, own case


def test_an_answer_before_the_question_or_to_another_question_does_not_count() -> None:
    other = format_question(Question(feature="P29", attempt=2, text="another"))
    assert parse_answer(["answer: too early", format_question(Q)], Q.question_id) is None
    assert parse_answer([other, "answer: for a2"], Q.question_id) is None
    assert parse_answer([format_question(Q), other, "answer: both"], Q.question_id) == "both"
    assert parse_answer([format_question(Q), "answer:   "], Q.question_id) is None


def test_the_offline_answer_file_round_trips(tmp_path: Path) -> None:
    assert read_answer_file(tmp_path, Q.question_id) is None
    path = write_answer_file(tmp_path, Q.question_id, "  both  ")
    assert path == tmp_path / ".scio" / "answers" / "P29-a1.md"
    assert read_answer_file(tmp_path, Q.question_id) == "both"
