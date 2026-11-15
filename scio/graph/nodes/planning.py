"""Planning nodes: ``scope`` (T2), parallel ``explore`` (T2), ``localize`` (T1), ``plan``,
``post_plan``.

Explorers are read-only and run in parallel through LangGraph ``Send``; their results
reduce into ``explorer_summaries`` as artifact references of at most ~1.5k tokens each.

Both planned tiers explore before they plan (issue #121): T2 runs ``scope`` and its
explorers, T1 runs one ``localize`` explorer, and the planner reads their summaries. Every
plan (the planner's, a replan's, the advisor's ``revised_steps``) then passes
:func:`keep_localized_files`: each open step's ``files_expected`` is grounded in the tree
(:func:`ground_plan`), T1 adds the files its explorer found to every open step so plan
drift compares the diff with files that exist (I-151, I-166), and a test filter that
selects nothing is flagged (:func:`unmatched_test_filters`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable
from pathlib import PurePosixPath
from typing import Any

from langgraph.types import Send

from scio.aci.toolbox import ToolBox
from scio.context.projections import (
    explore_message,
    localize_question,
    plan_checklist,
    plan_message,
    scope_message,
)
from scio.graph.effects import run_effect
from scio.graph.nodes.common import fact, git_out, untracked_files
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.loop.backends import make_worker

# The drift check's own normalization, so a path added here compares equal there.
from scio.loop.escalation import _norm as drift_norm
from scio.state.models import (
    ArtifactRef,
    ExitReason,
    ExplorerSummary,
    FeatureItem,
    FeatureStatus,
    Plan,
    PlannerOutput,
    PlanStep,
    Role,
    SCIOState,
    ScopeOutput,
    Tier,
)

Node = Callable[[SCIOState], dict[str, Any]]
EXPLORER_TOOLS = ("repo_search", "view_file")
SUMMARY_MAX_CHARS = 6000  # ~1.5k tokens
_ELLIPSIS = "..."
# A path with an extension, then ``:`` and a line number or a symbol (``calc.py:134``,
# ``graphcalc/names.py:check_not_constant``); a Windows drive prefix is allowed (I-151).
_CITATION_RE = re.compile(r"((?:[A-Za-z]:[\\/])?[\w./\\-]+\.[A-Za-z0-9]+):(?=:?\w)")
_ENTRY_RE = re.compile(r"(?:[A-Za-z]:[\\/])?[^:\s]+")
EXPLORE_SUMMARY_PROMPT = (
    "Stop exploring. Produce the JSON summary matching the schema: facts with file:line "
    "citations, the relevant files, and open questions. Be concise."
)
GENERIC_QUESTIONS = [
    "How are tests organized and run in this repository (framework, directories, fixtures)?",
    "Which modules and conventions (naming, docstrings, typing) must new code follow?",
]


def _tracked_files(ctx: RunContext) -> list[str]:
    # git_out returns a "(git ... failed: ...)" line on failure, which is no file.
    out = git_out(ctx.sandbox, "ls-files")
    return [f for f in out.splitlines() if f and not f.startswith("(git ")]


def _repo_stats(ctx: RunContext) -> tuple[str, str]:
    files = _tracked_files(ctx)
    src = sum(1 for f in files if ctx.toolchain.is_source(f))
    tree = "\n".join(files[:120]) + ("\n..." if len(files) > 120 else "")
    return f"{len(files)} tracked files, {src} {ctx.toolchain.name} source files", tree


def scope(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        stats, tree = _repo_stats(ctx)
        req = ctx.harness.build_request(
            "scoper",
            system=ctx.prompts.get("scoper").render(),
            messages=[Message.user(scope_message(state.task, repo_stats=stats, file_tree=tree))],
            tags={"thread": ctx.thread_id},
        )
        try:
            out: ScopeOutput = ctx.harness.structured(req, ScopeOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at scope: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        cfg = ctx.escalation
        questions = list(out.exploration_questions)
        for q in GENERIC_QUESTIONS:
            if len(questions) >= cfg.explorers_min:
                break
            questions.append(q)
        questions = questions[: cfg.explorers_max]
        features = state.feature_list or [
            FeatureItem(id=f"F{i + 1}", description=f.description)
            for i, f in enumerate(out.draft_features)
        ]
        entry = fact(f"scope: {len(questions)} exploration question(s)", source=Role.SCOPER)
        return {
            "exploration_questions": questions,
            "feature_list": features,
            "task_ledger": [entry],
            "phase": "explore",
            **ctx.budget_update(),
        }

    return node


def fan_out_explore(state: SCIOState) -> list[Send] | str:
    """Conditional edge after ``scope``: one ``explore`` task per question, in parallel."""
    if state.exit_reason is not None:
        return "halt"
    return [
        Send("explore", {"question": q, "index": i})
        for i, q in enumerate(state.exploration_questions)
    ]


def _shorten(text: str, over: int) -> str:
    """``text`` made at least ``over`` characters shorter in its JSON form ('' if it can't be)."""
    keep = len(text) - over - len(_ELLIPSIS)
    return text[:keep] + _ELLIPSIS if keep > 0 else ""


def summary_text(summary: ExplorerSummary, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """The summary as indented JSON of at most ``max_chars`` characters, always valid (I-152).

    Cutting the serialized text cut ``relevant_files`` and ``open_questions`` off a long
    summary and left invalid JSON. Over the cap, ``findings`` give way first: whole findings
    drop from the end, then the one left is shortened, and a closing entry in ``findings``
    says what was trimmed. The question (an echo of what was asked) is shortened next.
    ``open_questions`` and ``relevant_files`` lose items, from the end, only when they alone
    exceed the cap. Holds for any cap of at least 500 characters.
    """
    data: dict[str, Any] = summary.model_dump()
    text = json.dumps(data, indent=1)
    if len(text) <= max_chars:
        return text
    total = len(summary.findings)
    findings = list(summary.findings)
    notes: dict[str, str] = {}

    def render() -> str:
        dropped = total - len(findings)
        said = [f"{dropped} of {total} findings dropped"] if dropped else []
        note = f"[trimmed to the 1.5k-token cap: {'; '.join(said + list(notes.values()))}]"
        return json.dumps({**data, "findings": [*findings, note]}, indent=1)

    while len(findings) > 1 and len(render()) > max_chars:
        findings.pop()
    if findings and len(render()) > max_chars:
        notes["finding"] = "the last finding kept was shortened"
        findings[-1] = _shorten(findings[-1], len(render()) - max_chars)
        if not findings[-1]:
            findings.pop()
            del notes["finding"]
    if len(render()) > max_chars:
        notes["question"] = "the question was shortened"
        data["question"] = _shorten(str(data["question"]), len(render()) - max_chars)
    for key in ("open_questions", "relevant_files"):
        items, n = list(data[key]), len(data[key])
        while items and len(render()) > max_chars:
            items.pop()
            data[key] = items
            notes[key] = f"{n - len(items)} of {n} {key} dropped"
    return render()


def _explore_once(
    ctx: RunContext, question: str, *, tag: str
) -> tuple[ArtifactRef, ExplorerSummary]:
    """One read-only explorer run: the stored summary's artifact and the summary itself."""
    # Read-only context gathering must stay cheap: every turn resends the transcript (I-033).
    toolbox = ToolBox(ctx.sandbox, EXPLORER_TOOLS, human_gate=False, max_calls=8, role="explorer")
    loop = make_worker(ctx, toolbox, "explorer", max_model_calls=6)
    result = loop.run_typed(
        explore_message(question, repo_root=ctx.sandbox.root),
        ExplorerSummary,
        summary_prompt=EXPLORE_SUMMARY_PROMPT,
        tags={"thread": ctx.thread_id, "explore": tag},
    )
    summary: ExplorerSummary = result.output
    ref = ctx.artifacts.put(
        "summary", summary_text(summary), produced_by=Role.EXPLORER, name=f"explore-{tag}"
    )
    return ref, summary


def _resolve(raw: str, root: str | None, tracked: dict[str, str]) -> str | None:
    """The tracked path ``raw`` names: the same path, or the one tracked path it ends."""
    p = drift_norm(raw.strip("`'\" "), root)
    if not p:
        return None
    if p in tracked:
        return tracked[p]
    hits = {t for n, t in tracked.items() if n.endswith("/" + p)}
    return hits.pop() if len(hits) == 1 else None


def localized_files(
    summary: ExplorerSummary,
    *,
    tracked: Iterable[str],
    is_source: Callable[[str], bool],
    root: str | None = None,
) -> list[str]:
    """The files an explorer localized, as git spells them (I-151).

    Its ``relevant_files`` and the files its ``findings`` cite as ``path:line`` or
    ``path:symbol``; a bare file name counts when exactly one tracked path ends with it.
    Only tracked files the toolchain calls source stay: a planned file that does not exist
    is no evidence, and docs, configs and SCIO's own bookkeeping are not code to change.
    """
    by_norm = {drift_norm(t, None): t for t in tracked if t}
    cited: list[str] = []
    for entry in summary.relevant_files:
        m = _ENTRY_RE.search(entry.strip("`'\" "))
        if m:
            cited.append(m.group(0))
    cited += [m.group(1) for f in summary.findings for m in _CITATION_RE.finditer(f)]
    out: list[str] = []
    for raw in cited:
        path = _resolve(raw, root, by_norm)
        if path is not None and path not in out and is_source(path):
            out.append(path)
    return out


def add_localized_files(
    plan: Plan,
    files: list[str],
    *,
    root: str | None = None,
    named: Collection[str] = (),
) -> tuple[Plan, dict[str, list[str]]]:
    """``plan`` with ``files`` added to each open step's ``files_expected`` (I-151).

    Planned files stay. A step that names no files keeps none, since an empty
    ``files_expected`` turns the drift check off; a step in ``named`` (it named files that
    grounding then dropped, issue #121) gets them all the same. Returns the plan (same
    version) and the files added per step id.
    """
    added: dict[str, list[str]] = {}
    steps: list[PlanStep] = []
    for s in plan.steps:
        if not s.done and (s.files_expected or s.id in named):
            have = {drift_norm(f, root) for f in s.files_expected}
            new = [f for f in files if drift_norm(f, root) not in have]
            if new:
                added[s.id] = new
                s = s.model_copy(update={"files_expected": [*s.files_expected, *new]})
        steps.append(s)
    return plan.model_copy(update={"steps": steps}), added


def _localize_plan(
    ctx: RunContext, plan: Plan, summary: ExplorerSummary, *, named: Collection[str] = ()
) -> tuple[Plan, str]:
    files = localized_files(
        summary,
        tracked=_tracked_files(ctx),
        is_source=ctx.toolchain.is_source,
        root=ctx.sandbox.root,
    )
    new_plan, added = add_localized_files(plan, files, root=ctx.sandbox.root, named=named)
    if not files:
        return new_plan, "no files added to the plan (the explorer named no tracked source file)"
    if not added:
        return new_plan, (
            f"no files added to the plan: the explorer localized {', '.join(files)}, but each "
            "open step already lists them or names no files (drift check off)"
        )
    per_step = "; ".join(f"{sid} += {', '.join(fs)}" for sid, fs in added.items())
    return new_plan, f"files the explorer localized added to the plan: {per_step}"


def explore(ctx: RunContext) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Fan-out node: input is the ``Send`` payload, output reduces into ``explorer_summaries``."""

    def node(payload: dict[str, Any]) -> dict[str, Any]:
        question = str(payload["question"])
        ref, _summary = _explore_once(ctx, question, tag=str(payload.get("index", 0)))
        return {"explorer_summaries": [ref]}

    return node


def localize(ctx: RunContext) -> Node:
    """T1: a single explorer answers where the task should be implemented and tested,
    before the first plan, and ``plan`` reads its summary (issue #121). It runs once per
    run: replans (the advisor's, the gate's) return to ``plan``, never here. The files it
    found join every open step's ``files_expected`` in :func:`keep_localized_files` (I-151).

    A checkpoint from before issue #121 can still reach it after ``post_plan``; then it
    adds its files to that plan here, as it did, and :func:`after_localize` goes on to
    ``implement``."""

    def node(state: SCIOState) -> dict[str, Any]:
        question = localize_question(state.current_step(), state.task)
        try:
            ref, summary = _explore_once(ctx, question, tag="localize")
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at localize: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        entries = [fact("localize: repository context gathered", source=Role.EXPLORER)]
        update: dict[str, Any] = {"phase": "plan"}
        if state.plan is not None:
            update["plan"], note = _localize_plan(ctx, state.plan, summary)
            update["phase"] = "implement"
            entries.append(fact(f"localize: {note}", source=Role.SYSTEM))
        return {
            **update,
            "explorer_summaries": [ref],
            "task_ledger": entries,
            **ctx.budget_update(),
        }

    return node


def after_localize(state: SCIOState) -> str:
    """``plan`` with the explorer's summary; ``implement`` when a plan exists already (a
    checkpoint from before issue #121, where localize ran after ``post_plan``)."""
    if state.exit_reason is not None:
        return "halt"
    return "implement" if state.plan is not None else "plan"


_NOOP_STEP_RE = re.compile(
    r"\b(explore|exploration|investigate|inspect|read-only|no code changes|no changes|"
    r"verify the current state|locate)\b",
    re.IGNORECASE,
)


def normalize_steps(
    steps: list[PlanStep], feature_ids: list[str]
) -> tuple[list[PlanStep], list[str]]:
    """Make a plan executable (ledger I-039).

    Drops steps that carry no feature or describe exploration / no-op work (verify can never
    pass a step that changes nothing), and appends a final step for every feature the plan
    left uncovered. Returns the steps and human-readable notes of what changed.
    """
    notes: list[str] = []
    kept: list[PlanStep] = []
    for s in steps:
        if not s.feature_ids:
            notes.append(f"plan step {s.id} dropped: no feature ids ({s.description[:60]})")
            continue
        if _NOOP_STEP_RE.search(s.description) and not re.search(
            r"\b(add|implement|write|fix|change|create|update|refactor|remove)\b",
            s.description,
            re.IGNORECASE,
        ):
            notes.append(
                f"plan step {s.id} dropped: exploration is not a plan step ({s.description[:60]})"
            )
            continue
        kept.append(s)
    covered = {fid for s in kept for fid in s.feature_ids}
    missing = [fid for fid in feature_ids if fid not in covered]
    if missing:
        files = sorted({f for s in kept for f in s.files_expected})
        next_id = f"S{len(kept) + 1}"
        while any(s.id == next_id for s in kept):
            next_id = f"S{int(next_id[1:]) + 1}"
        kept.append(
            PlanStep(
                id=next_id,
                description=f"Implement and test the remaining features {', '.join(missing)}",
                feature_ids=missing,
                files_expected=files,
            )
        )
        notes.append(f"plan step {next_id} added for uncovered features {', '.join(missing)}")
    return kept, notes


def summaries_text(ctx: RunContext, state: SCIOState) -> list[str]:
    # summary_text keeps every stored summary within SUMMARY_MAX_CHARS, so this never cuts.
    return [ctx.artifacts.head(ref, SUMMARY_MAX_CHARS) for ref in state.explorer_summaries]


def _stored_summary(ctx: RunContext, ref: ArtifactRef) -> ExplorerSummary | None:
    """A summary read back from its artifact; None for one the old character cut broke."""
    try:
        return ExplorerSummary.model_validate_json(ctx.artifacts.get(ref))
    except (OSError, ValueError):
        return None


_WRITES_RE = re.compile(
    r"\b(add|adds|adding|create|creates|creating|write|writes|writing|new|tests?)\b",
    re.IGNORECASE,
)
"""A step whose text says it writes something: a test path it names may be new (#121)."""
_FILTER_TERM_RE = re.compile(r"[\w.\-]+")
_FILTER_KEYWORDS = frozenset({"and", "or", "not"})


def _ancestors(paths: Iterable[str]) -> set[str]:
    """Every directory that holds one of ``paths`` (``a/b/c.py`` gives ``a`` and ``a/b``)."""
    out: set[str] = set()
    for p in paths:
        parts = p.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts)))
    return out


def _parent(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _grounded_match(
    path: str, *, localized: list[str], known: dict[str, str], listed: set[str]
) -> tuple[str, str] | None:
    """The obvious existing file a missing ``path`` meant, and where it came from, or None.

    1. ``tree``: the one file in the tree whose path ends with ``path`` (spelled short);
    2. ``localized``: the one file the explorer found with the same basename;
    3. ``localized``: the one file the explorer found in the same directory that the step
       does not list already. Anything less certain is no match.
    """
    hit = _resolve(path, None, known)
    if hit is not None:
        return hit, "tree"
    name = PurePosixPath(path).name
    same = [f for f in localized if PurePosixPath(f).name == name]
    if len(same) == 1:
        return same[0], "localized"
    parent = _parent(path)
    if parent:
        near = [
            f
            for f in localized
            if _parent(drift_norm(f, None)) == parent and drift_norm(f, None) not in listed
        ]
        if len(near) == 1:
            return near[0], "localized"
    return None


def ground_plan(
    plan: Plan,
    *,
    tree: Iterable[str],
    localized: list[str],
    is_test_path: Callable[[str], bool],
    root: str | None = None,
) -> tuple[Plan, list[str], set[str]]:
    """``plan`` with each open step's ``files_expected`` grounded in ``tree`` (issue #121).

    ``tree`` is every file git sees (tracked and untracked, as git spells them). A named
    path stays when it exists (a file, or a directory holding one), when the step declares
    it in ``files_new``, or when it is a test path (``is_test_path``) and the step's text
    says it writes something (add, create, write, new, test). Any other path is a guess: it
    is replaced by the obvious file it meant (:func:`_grounded_match`; each match stands in
    for one guess); else kept as a probable new file when its directory exists (the
    repository root counts), since dropping a file the coder then creates would be plan
    drift; else dropped. ``files_new`` entries join ``files_expected``, since plan drift
    reads only that list.

    Returns the plan (same version), one note per replacement, kept guess or drop (``S1
    a.py -> b.py (localized)``), and the ids of the open steps that named files before
    grounding, so T1's localized files still join a step whose every guess was dropped
    (an empty list would turn drift off for it).
    """
    known = {drift_norm(t, None): t for t in tree if t}
    dirs = _ancestors(known)
    notes: list[str] = []
    named: set[str] = set()
    steps: list[PlanStep] = []
    for s in plan.steps:
        if s.done:
            steps.append(s)
            continue
        expected = list(dict.fromkeys([*s.files_expected, *s.files_new]))
        if expected:
            named.add(s.id)
        declared = {drift_norm(f, root) for f in s.files_new}
        writes = bool(_WRITES_RE.search(s.description))
        listed = {drift_norm(f, root) for f in expected}
        kept: dict[str, str] = {}  # normalized -> as spelled, in order, without duplicates
        for f in expected:
            p = drift_norm(f.strip("`'\" "), root)
            exists = p in known or p.rstrip("/") in dirs
            if not p or exists or p in declared or (writes and is_test_path(p)):
                kept.setdefault(p, f)
                continue
            match = _grounded_match(p, localized=localized, known=known, listed=listed)
            if match is not None:
                notes.append(f"{s.id} {f} -> {match[0]} ({match[1]})")
                kept.setdefault(drift_norm(match[0], None), match[0])
                listed.add(drift_norm(match[0], None))  # a file stands in for one guess only
            elif not _parent(p) or _parent(p) in dirs:
                # Never drop a plausible new file: one the coder then creates would drift,
                # while an expected file nobody touches costs nothing (P26 created state.py).
                notes.append(f"{s.id} kept {f} (probable new file: its directory exists)")
                kept.setdefault(p, f)
            else:
                notes.append(
                    f"{s.id} {f} dropped (not in the tree, not declared new, and its "
                    "directory does not exist)"
                )
        out = list(kept.values())
        if expected and not out:
            notes.append(f"{s.id} keeps none of the files it named")
        if out != s.files_expected:
            s = s.model_copy(update={"files_expected": out})
        steps.append(s)
    return plan.model_copy(update={"steps": steps}), notes, named


def _test_filter_terms(test_filter: str) -> tuple[str | None, list[str]]:
    """A filter that names a path (``tests/test_x.py``, ``tests/x.test.ts::name``) as that
    path; else the terms it selects on, without ``and``/``or`` and anything after ``not``."""
    text = test_filter.strip()
    if text and not any(c.isspace() for c in text) and ("/" in text or "::" in text):
        return text.split("::", 1)[0], []
    terms: list[str] = []
    negated = False
    for term in _FILTER_TERM_RE.findall(text):
        low = term.lower()
        if low in _FILTER_KEYWORDS:
            negated = low == "not"
            continue
        if not negated and len(term) >= 2 and term not in terms:
            terms.append(term)
        negated = False
    return None, terms


def unmatched_test_filters(
    plan: Plan,
    features: list[FeatureItem],
    *,
    tree: Iterable[str],
    is_test_path: Callable[[str], bool],
    files_containing: Callable[[list[str]], set[str] | None],
    root: str | None = None,
) -> list[str]:
    """One note per open feature whose ``test_filter`` selects nothing (issue #121).

    A path filter matches when the path exists or its step names it (after
    :func:`ground_plan`, a named path that does not exist is one the step creates). A term
    filter matches when a term is in a test file's path or text (``files_containing``: the
    files whose text holds any term, None when that cannot be read), or when its step
    creates a test file. A filter is flagged, never dropped: the coder may write the test,
    and a halt on "no tests collected" then reads against the note.
    """
    paths = [drift_norm(t, None) for t in tree if t]
    have = set(paths) | _ancestors(paths)
    tests = [p for p in paths if is_test_path(p)]
    by_id = {f.id: f for f in features}
    checked: set[str] = set()
    notes: list[str] = []
    for s in plan.steps:
        if s.done:
            continue
        named = [drift_norm(f, root) for f in s.files_expected]
        new_tests = [p for p in named if p not in have and is_test_path(p)]
        for fid in s.feature_ids:
            f = by_id.get(fid)
            if fid in checked or f is None or not f.test_filter:
                continue
            checked.add(fid)
            if f.status is FeatureStatus.PASSING:
                continue
            path, terms = _test_filter_terms(f.test_filter)
            if path is not None:
                p = drift_norm(path, root)
                matched = p.rstrip("/") in have or p in named
            elif not terms:
                continue
            elif new_tests or any(t.lower() in tp.lower() for t in terms for tp in tests):
                matched = True
            else:
                hits = files_containing(terms)
                if hits is None:
                    continue  # unknown is not "no match"
                matched = any(is_test_path(drift_norm(h, None)) for h in hits)
            if not matched:
                notes.append(
                    f"{fid}'s test filter {f.test_filter!r} ({s.id}) matches no test in the "
                    f"tree, and {s.id} creates no test file; kept (the coder may write it)"
                )
    return notes


def _files_containing(ctx: RunContext, terms: list[str]) -> set[str] | None:
    """The files (tracked or untracked) whose text holds any of ``terms``, ignoring case;
    None when git cannot say."""
    argv = ["git", "grep", "--untracked", "-I", "-i", "-l", "-F"]
    for t in terms:
        argv += ["-e", t]
    r = ctx.sandbox.exec(argv, timeout_s=60)
    if r.exit_code == 1 and not r.stderr.strip() and not r.timed_out:
        return set()  # git grep's "no match"
    if not r.ok:
        return None
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def keep_localized_files(
    ctx: RunContext,
    state: SCIOState,
    plan: Plan,
    *,
    features: list[FeatureItem] | None = None,
) -> tuple[Plan, list[str]]:
    """Every new plan passes here: the planner's (v1 and each replan) and the advisor's
    ``revised_steps`` (implement.py). Returns the plan and the notes to record.

    1. Its open steps' files are grounded in the tree (:func:`ground_plan`, issue #121),
       with the files every stored explorer summary localized as the obvious matches.
    2. T1: the localize explorer's files are merged into its open steps (I-151). Every
       replan names its files again without exploring, and localize runs once per run
       (build.py), so the files it found would drop out and drift would fire on them again.
    3. A test filter that selects nothing is flagged (:func:`unmatched_test_filters`);
       ``features`` defaults to the state's (the advisor's replan keeps them).
    """
    root = ctx.sandbox.root
    tracked = _tracked_files(ctx)
    # untracked too: an advisor's replan can name a file the coder created this step
    tree = list(dict.fromkeys([*tracked, *untracked_files(ctx.sandbox)]))
    stored = [_stored_summary(ctx, ref) for ref in state.explorer_summaries]
    summaries = [x for x in stored if x is not None]
    localized: list[str] = []
    for summary in summaries:
        found = localized_files(
            summary, tracked=tracked, is_source=ctx.toolchain.is_source, root=root
        )
        localized += [f for f in found if f not in localized]
    plan, grounding, named = ground_plan(
        plan, tree=tree, localized=localized, is_test_path=ctx.toolchain.is_test_path, root=root
    )
    notes = [f"plan grounding (v{plan.version}): {n}" for n in grounding]
    if state.routing is not None and state.routing.tier is Tier.T1:
        for summary in summaries:
            plan, note = _localize_plan(ctx, plan, summary, named=named)
            notes.append(f"plan v{plan.version}: {note}")
    notes += [
        f"plan grounding (v{plan.version}): {n}"
        for n in unmatched_test_filters(
            plan,
            state.feature_list if features is None else features,
            tree=tree,
            is_test_path=ctx.toolchain.is_test_path,
            files_containing=lambda terms: _files_containing(ctx, terms),
            root=root,
        )
    ]
    return plan, notes


def plan(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        previous = state.plan
        reason = state.advisor_guidance if previous is not None else None
        msg = plan_message(
            state.task,
            state.feature_list,
            summaries=summaries_text(ctx, state),
            previous=previous,
            revision_reason=reason,
        )
        req = ctx.harness.build_request(
            "planner",
            system=ctx.prompts.get("planner").render(),
            messages=[Message.user(msg)],
            tags={
                "thread": ctx.thread_id,
                "plan_version": str((previous.version if previous else 0) + 1),
            },
        )
        try:
            out: PlannerOutput = ctx.harness.structured(req, PlannerOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at plan: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}

        # Merge the planner's feature list into ours: keep status/commit, take filters/text.
        existing = {f.id: f for f in state.feature_list}
        features: list[FeatureItem] = []
        for pf in out.feature_list:
            cur = existing.get(pf.id)
            if cur is None:
                features.append(pf)
            else:
                features.append(
                    cur.model_copy(
                        update={
                            "description": pf.description or cur.description,
                            "test_filter": pf.test_filter or cur.test_filter,
                        }
                    )
                )
        for fid, f in existing.items():
            if fid not in {x.id for x in features}:
                features.append(f)
        known = {f.id for f in features}
        steps = []
        done_ids = {s.id for s in previous.steps if s.done} if previous else set()
        for s in out.steps:
            steps.append(
                s.model_copy(
                    update={
                        "feature_ids": [i for i in s.feature_ids if i in known],
                        "done": s.id in done_ids,
                    }
                )
            )
        steps, plan_notes = normalize_steps(steps, [f.id for f in features])
        if reason and reason.startswith("gate replan") and all(s.done for s in steps):
            # A gate replan must produce one more implementation pass; a plan whose steps
            # are all done would hit "implement: no open plan step" and halt (I-043).
            steps.append(
                PlanStep(
                    id=f"S{len(steps) + 1}",
                    description=f"Address the gate's unmet criteria: {reason[:1500]}",
                    feature_ids=[f.id for f in features],
                    files_expected=[],
                )
            )
            plan_notes.append("gate replan: appended a step because every step was done")
        new_plan = Plan(
            version=(previous.version + 1) if previous else 1,
            author=Role.PLANNER,
            steps=steps,
            rationale=out.rationale,
            revision_reason=reason,
        )
        first_open = next((s.id for s in steps if not s.done), None)
        progress = state.progress.model_copy(
            update={
                "current_step_id": first_open,
                "attempts_on_step": 0,
                "fix_attempts_on_error": 0,
                "last_error_signature": None,
            }
        )
        entries = [
            fact(
                f"plan v{new_plan.version}: {len(steps)} step(s); "
                f"risks: {'; '.join(out.risks)[:200]}",
                source=Role.PLANNER,
            )
        ] + [fact(n, source=Role.SYSTEM) for n in plan_notes]
        # grounded in the tree, then T1's localized files merged back (issue #121, I-151)
        new_plan, notes = keep_localized_files(ctx, state, new_plan, features=features)
        entries += [fact(n, source=Role.SYSTEM) for n in notes]
        return {
            "plan": new_plan,
            "feature_list": features,
            "progress": progress,
            "advisor_guidance": None,
            "task_ledger": entries,
            "phase": "post_plan",
            **ctx.budget_update(),
        }

    return node


def after_plan(state: SCIOState) -> str:
    return "halt" if state.exit_reason is not None else "post_plan"


def post_plan(ctx: RunContext) -> Node:
    """Mirror the plan to the GitHub issue as a checklist (idempotent by marker)."""

    def node(state: SCIOState) -> dict[str, Any]:
        assert state.plan is not None
        body = plan_checklist(state.plan, state.feature_list)
        ref = ctx.artifacts.put(
            "plan_comment", body, produced_by=Role.PLANNER, name=f"plan-v{state.plan.version}"
        )
        github = state.github
        if ctx.github is not None and state.task.issue_number:
            issue = state.task.issue_number
            cid = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="post_plan",
                intent=f"plan_comment:v{state.plan.version}",
                apply=lambda: (
                    ctx.github.upsert_issue_comment(issue, body, marker="scio:plan")
                    if ctx.github
                    else 0
                ),
            )
            github = github.model_copy(update={"plan_comment_id": cid})
            entry = fact(f"plan v{state.plan.version} posted to issue #{issue}", source=Role.SYSTEM)
        else:
            entry = fact("plan stored as artifact (no issue to comment on)", source=Role.SYSTEM)
        return {
            "github": github,
            "artifacts": {**state.artifacts, f"plan:v{state.plan.version}": ref},
            "task_ledger": [entry],
            "phase": "implement",
        }

    return node
