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


def test_a_given_qid_is_the_question_id_everywhere(tmp_path: Path) -> None:
    """The build passes the attempt's thread id: the marker, the answer and the answer file
    all use it, and an answer to the same feature and attempt under another id is not it."""
    m4 = Question("P29", 1, "Which rule?", qid="build-graphcalc-m4-p29-a1")
    m5 = Question("P29", 1, "Which rule?", qid="build-graphcalc-m5-p29-a1")
    assert m4.question_id == "build-graphcalc-m4-p29-a1" and Q.question_id == "P29-a1"
    assert format_question(m4).splitlines()[0] == (
        "<!-- scio:question feature=P29 attempt=1 id=build-graphcalc-m4-p29-a1 -->"
    )
    comments = [format_question(m4), "answer: rule A", format_question(m5)]
    assert parse_answer(comments, m4.question_id) == "rule A"
    assert parse_answer(comments, m5.question_id) is None
    write_answer_file(tmp_path, m4.question_id, "rule A")
    assert read_answer_file(tmp_path, m5.question_id) is None
