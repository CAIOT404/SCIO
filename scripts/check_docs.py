"""Docs checks. Run in CI; a failure here blocks merge like a test failure.

Checks (M1):
1. Relative links in Markdown files resolve to existing files.
2. Model IDs appear only in ``configs/models.yaml`` (and docs). Code refers to roles.
3. ``CHANGELOG.md`` has an ``[Unreleased]`` section.

M2 adds: architecture diagram vs. compiled graph, runnable code samples, public-API
docstring coverage. Issue #61 adds the guardrails check: the Always/Never rules cite every
resolved ledger entry from I-104 on, and every guard they name exists.

Issue #122 (W-38 to W-41) makes the ledger, the guardrails and the unreleased changes
fragments, one file each: ``docs/ledger/I-NNN.md``, ``docs/guardrails/NNN.md`` and
``changes/unreleased/<n>-<slug>.<kind>.md``. Every check here reads them there, and fails on
an entry, rule or change still written into ``docs/ISSUE_LEDGER.md``,
``docs/LESSONS_LEARNED.md`` or CHANGELOG.md's ``[Unreleased]``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")
MODEL_ID_RE = re.compile(r"\b(claude-[a-z]+-\d(?:-\d)?|gpt-\d[\w.-]*)\b")
CODE_DIRS = ("scio", "evals", "tests", "scripts", "recovery")
SKIP_DIRS = {
    ".git",
    ".scio",  # runtime state: checkpoints, kept trials, the live-run worktree
    ".venv",
    "node_modules",
    "__pycache__",
    "docs/api",
    "evals/results",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".claude/worktrees",  # other checkouts of this repo (parallel agents, I-155): not ours
}
NEVER_OURS = {".venv", "node_modules", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"}
"""Directory names whose contents are never this repository's docs, at any depth (I-155)."""


def _skip(p: Path) -> bool:
    rel = p.relative_to(ROOT).as_posix()
    if any(part in NEVER_OURS for part in rel.split("/")[:-1]):
        return True
    return any(rel == s or rel.startswith(s + "/") for s in SKIP_DIRS)


FENCE_RE = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL)
"""A fenced code block, indented ones in a list item included (I-201)."""
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans; links inside code are not links."""
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))


def links_in(md: Path, base: Path | None = None) -> list[str]:
    """Broken relative links in one Markdown file, as error strings. A link is resolved
    from ``base``, the file's own folder by default."""
    errors: list[str] = []
    rel = md.relative_to(ROOT).as_posix() if md.is_relative_to(ROOT) else md.name
    text = _strip_code(md.read_text(encoding="utf-8", errors="replace"))
    base = base if base is not None else md.parent
    for m in LINK_RE.finditer(text):
        target = m.group(1)
        if re.match(r"^[a-z]+:", target) or target.startswith("mailto:"):
            continue
        # Windows resolves '..', '...' etc. to parent directories; only '..' is a real link.
        if set(target) == {"."} and target != "..":
            errors.append(f"{rel}: broken link -> {target}")
            continue
        if not (base / target).exists():
            errors.append(f"{rel}: broken link -> {target}")
    return errors


def check_links() -> list[str]:
    """Every relative link in our Markdown resolves. A change fragment's text is read from
    the repository's root: the release cut moves it into CHANGELOG.md there (W-40)."""
    errors: list[str] = []
    changes = ROOT / CHANGES_DIR
    for md in ROOT.rglob("*.md"):
        if not _skip(md):
            errors.extend(links_in(md, ROOT if md.parent == changes else None))
    return errors


def check_model_ids() -> list[str]:
    errors: list[str] = []
    for d in CODE_DIRS:
        for f in (ROOT / d).rglob("*.py"):
            if _skip(f):
                continue
            for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if MODEL_ID_RE.search(line) and "noqa: model-id" not in line:
                    errors.append(
                        f"{f.relative_to(ROOT)}:{i}: model ID in code; "
                        "use a role from configs/models.yaml"
                    )
    return errors


CHANGES_DIR = Path("changes") / "unreleased"
"""The unreleased changes, one file per change (issue #122, W-40)."""
MIGRATE = "uv run python scripts/migrate_fragments.py"


def _release_notes() -> ModuleType:
    """``scripts/release_notes.py``, the reader of the change fragments, loaded by path from
    this file's own folder (``ROOT`` may point at another checkout in a test)."""
    path = Path(__file__).resolve().parent / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def unreleased_changes(root: Path | None = None) -> str:
    """The unreleased changes' text: every change fragment, in release order."""
    rn = _release_notes()
    folder = (root or ROOT) / CHANGES_DIR
    return "\n".join(p.read_text(encoding="utf-8") for p in rn.change_fragments(folder))


def change_fragment_problems(root: Path | None = None) -> list[str]:
    """Each file in ``changes/unreleased/`` is a well-named change fragment holding a
    bullet's text, and CHANGELOG.md's ``[Unreleased]`` holds no change of its own."""
    root = root or ROOT
    rn = _release_notes()
    folder = root / CHANGES_DIR
    rel = CHANGES_DIR.as_posix()
    errors = [
        f"{rel}/{p.name}: not a change fragment's name; it is <n>-<slug>.<added|changed|fixed>.md "
        "(changes/README.md)"
        for p in rn.stray_files(folder)
    ]
    for p in rn.change_fragments(folder):
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            errors.append(f"{rel}/{p.name}: is empty")
        elif text.lstrip().startswith("- "):
            errors.append(
                f"{rel}/{p.name}: starts with '- '; a fragment holds the bullet's text, and the "
                "release cut adds the marker"
            )
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    parts = SECTION_RE.split(changelog)
    unreleased = dict(zip(parts[1::2], parts[2::2], strict=True)).get("Unreleased", "")
    errors += [
        f"CHANGELOG.md: [Unreleased] holds a change ({ln.strip()[:60]!r}); a change is its own "
        f"file in {rel}/ ({MIGRATE} moves it)"
        for ln in unreleased.splitlines()
        if ln.startswith("- ")
    ]
    return errors


def check_changelog(root: Path | None = None) -> list[str]:
    """CHANGELOG.md has its ``[Unreleased]`` section, the pointer the release cut writes
    under, and the unreleased changes are well-formed fragments (W-40)."""
    root = root or ROOT
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    errors = (
        [] if "## [Unreleased]" in text else ["CHANGELOG.md: missing '## [Unreleased]' section"]
    )
    return errors + change_fragment_problems(root)


MILESTONE_RE = re.compile(r"\bM(\d+)\b")
SECTION_RE = re.compile(r"^## \[([^\]]+)\][^\n]*$", re.MULTILINE)


def _product_mode_status(root: Path | None = None) -> str:
    pm = ((root or ROOT) / "docs" / "PRODUCT_MODE.md").read_text(encoding="utf-8")
    return next((ln for ln in pm.splitlines() if ln.startswith("- **Status:**")), "")


def unreleased_done_milestones(
    changelog: str, pm_status: str, unreleased: str | None = None
) -> list[str]:
    """Milestones the PRODUCT_MODE status line calls done that the unreleased changes name
    and no released section does (I-086). The unreleased changes are ``unreleased`` (the
    change fragments' text) when given, else CHANGELOG.md's ``[Unreleased]`` section."""
    done = set(re.findall(r"\b(M\d+) [A-Za-z +\-]+? done\b", pm_status))
    parts = SECTION_RE.split(changelog)
    bodies = dict(zip(parts[1::2], parts[2::2], strict=True))
    section = bodies.pop("Unreleased", "")
    unreleased = section if unreleased is None else unreleased
    released = "\n".join(bodies.values())
    named = {f"M{n}" for n in MILESTONE_RE.findall(unreleased)}
    named -= {f"M{n}" for n in MILESTONE_RE.findall(released)}
    return sorted(done & named, key=lambda m: int(m[1:]))


def check_release_cadence(root: Path | None = None) -> list[str]:
    """A release follows every milestone merge: one finished milestone may wait in the
    unreleased changes for its release PR, two may not (I-086: M8 to M10 piled up behind
    v0.5.0 for two weeks). The unreleased changes are the fragments (W-40)."""
    root = root or ROOT
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    pending = unreleased_done_milestones(
        changelog, _product_mode_status(root), unreleased_changes(root)
    )
    if len(pending) < 2:
        return []
    return [
        f"CHANGELOG.md: {', '.join(pending)} are done but unreleased; cut a release after "
        "each milestone merges (I-086)"
    ]


def released_section_changed(current: str, tagged: str, version: str) -> bool:
    """True when ``version``'s CHANGELOG section differs from the one its tag shipped."""
    parts = SECTION_RE.split(current)
    now = dict(zip(parts[1::2], parts[2::2], strict=True)).get(version)
    parts = SECTION_RE.split(tagged)
    then = dict(zip(parts[1::2], parts[2::2], strict=True)).get(version)
    return then is not None and (now or "").strip() != then.strip()


def check_released_section_frozen(root: Path | None = None) -> list[str]:
    """The newest released CHANGELOG section stays what its tag shipped (I-089): a branch
    that merges ``main`` across a release cut gets its own ``[Unreleased]`` entries moved
    under the new version by git's merge, silently. Skipped before the tag exists (the
    release PR itself) and in a clone without tags."""
    import subprocess

    root = root or ROOT
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    if not m:
        return []
    version = m.group(1)
    proc = subprocess.run(
        ["git", "show", f"v{version}:CHANGELOG.md"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        return []
    if released_section_changed(changelog, proc.stdout, version):
        return [
            f"CHANGELOG.md: the [{version}] section differs from tag v{version}; new entries "
            "belong under [Unreleased] (I-089)"
        ]
    return []


def check_readme_status() -> list[str]:
    """The README is the repository's landing page and must say where the project stands at
    every push (owner's rule, 2026-10-26). Its ``> Status:`` line names the latest released
    version from CHANGELOG.md and every milestone docs/PRODUCT_MODE.md calls ``in progress``;
    its ADR list names the newest ADR."""
    errors: list[str] = []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status = next((ln for ln in readme.splitlines() if ln.startswith("> Status:")), "")
    if not status:
        return ["README.md: no '> Status:' line (the landing page must say where we are)"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    if released and f"v{released.group(1)} released" not in status:
        errors.append(
            f"README.md status line does not say 'v{released.group(1)} released' "
            "(CHANGELOG.md's latest release)"
        )
    pm_status = _product_mode_status()
    for milestone in re.findall(r"(M\d+) [A-Za-z +\-]+? in progress", pm_status):
        if not re.search(rf"\b{milestone}\b.{{0,40}}in progress", status):
            errors.append(
                f"README.md status line does not say {milestone} is in progress, as "
                "docs/PRODUCT_MODE.md does"
            )
    adrs = sorted(p.name[:4] for p in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
    adr_line = next((ln for ln in readme.splitlines() if "docs/adr/" in ln and "0001" in ln), "")
    if adrs and adrs[-1] not in adr_line:
        errors.append(f"README.md ADR list does not name the newest ADR {adrs[-1]}")
    return errors


def check_ignored_sources() -> list[str]:
    """No source file under the code directories may be git-ignored (ledger I-017)."""
    import subprocess

    proc = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", *CODE_DIRS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []  # not a git checkout (e.g. an sdist); nothing to check
    ignored = [
        ln for ln in proc.stdout.splitlines()
        if ln.endswith((".py", ".md", ".yaml", ".yml"))
        and "__pycache__" not in ln
        and not any(ln.startswith(s + "/") for s in SKIP_DIRS)  # generated outputs are fine
    ]  # fmt: skip
    return [f"{ln}: source file is git-ignored; fix .gitignore" for ln in ignored]


def check_graph_sections() -> list[str]:
    """docs/ARCHITECTURE.md generated Mermaid sections must match the compiled graphs."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_mermaid

    return gen_mermaid.check()


SCHEDULED_TRIGGER_RE = re.compile(r"New-ScheduledTaskTrigger\b[^\n]*")


def logon_trigger_problems(label: str, text: str) -> list[str]:
    """A Task Scheduler logon trigger in a PowerShell recipe names its user (I-200).
    Without ``-User`` it fires at any user's logon, which only an administrator may
    register. A line continued with a backtick is read as one line."""
    joined = text.replace("`\r\n", " ").replace("`\n", " ")
    return [
        f"{label}: a logon trigger without -User is refused outside an administrator's "
        "shell; name the user, as docs/RECOVERY.md does (I-200)"
        for m in SCHEDULED_TRIGGER_RE.finditer(joined)
        if "-AtLogOn" in m.group(0) and "-User" not in m.group(0)
    ]


def markdown_code(text: str) -> str:
    """The fenced code blocks of a Markdown text, joined; prose is left out."""
    return "\n".join(m.group(0) for m in FENCE_RE.finditer(text))


def check_scheduled_task_recipes() -> list[str]:
    """Every Task Scheduler recipe in a Markdown code block or a script registers without an
    administrator (I-200). This file is not scanned: its pattern is not a recipe."""
    errors: list[str] = []
    for md in ROOT.rglob("*.md"):
        if not _skip(md):
            text = md.read_text(encoding="utf-8", errors="replace")
            label = md.relative_to(ROOT).as_posix()
            errors.extend(logon_trigger_problems(label, markdown_code(text)))
    for py in (ROOT / "scripts").rglob("*.py"):
        if py.name != "check_docs.py" and not _skip(py):
            text = py.read_text(encoding="utf-8", errors="replace")
            errors.extend(logon_trigger_problems(py.relative_to(ROOT).as_posix(), text))
    return errors


GUARDRAILS_FROM = 104  # every resolved ledger entry from here on is owed a rule (issue #61)
LEDGER_ENTRY_RE = re.compile(r"^### (I-(\d+)) ", re.MULTILINE)
GUARDRAIL_START_RE = re.compile(r"^\d+\. \*\*(Always|Never)\*\* ")
GUARD_RE = re.compile(r"`(symbol|test|check|hook|manual):([^`]*)`")


def guardrail_rules(lessons: str) -> list[str]:
    """The numbered lines of ``## Guardrails`` in LESSONS_LEARNED.md, one rule each."""
    if "\n## Guardrails" not in lessons:
        return []
    section = lessons.split("\n## Guardrails", 1)[1].split("\n## ", 1)[0]
    return [ln for ln in section.splitlines() if re.match(r"^\d+\. ", ln)]


def resolved_ledger_ids(ledger: str, start: int = GUARDRAILS_FROM) -> list[str]:
    """Ids of the ledger entries numbered ``start`` or later whose status is resolved."""
    parts = LEDGER_ENTRY_RE.split(ledger)
    entries = zip(parts[1::3], parts[2::3], parts[3::3], strict=True)
    return [i for i, n, body in entries if int(n) >= start and "Status: resolved" in body]


STATUS_LINE_RE = re.compile(r"^- Resolved: [^\n]*· Status: \S", re.MULTILINE)
STATUS_RE = re.compile(r"^- Resolved: [^\n]*· Status: (\w+)", re.MULTILINE)
TODO_FIELD_RE = re.compile(
    r"^- (?:Found|Symptom|Root cause|Fix|Sweep|Guard):(?: [^\n]*·)? TODO\b", re.MULTILINE
)
"""A field ``ledger_new.py`` left as ``TODO`` (``- Fix: TODO``, ``- Found: <date> · TODO ·
<where>``). A field whose prose mentions a TODO is not one."""


def ledger_status_problems(ledger: str) -> list[str]:
    """Every ledger entry has its ``- Resolved: <date or -> · Status: <status>`` line. An
    entry without one is neither open nor resolved, so ``check_guardrails`` never asks it
    for a rule (I-204). An entry that is no longer open has no ``TODO`` field left (W-41)."""
    parts = LEDGER_ENTRY_RE.split(ledger)
    entries = zip(parts[1::3], parts[2::3], parts[3::3], strict=True)
    problems = []
    for lid, _n, body in entries:
        status = STATUS_RE.search(body)
        if not STATUS_LINE_RE.search(body):
            problems.append(f"{lid} has no '- Resolved: <date or -> . Status: <status>' line")
        elif status and status.group(1) != "open" and TODO_FIELD_RE.search(body):
            problems.append(
                f"{lid} is {status.group(1)} but a field still says TODO; fill it in before "
                "closing the entry"
            )
    return problems


LEDGER_DIR = Path("docs") / "ledger"
"""The ledger: one file per entry, ``I-NNN.md`` (issue #122, W-38). Its README holds the
template; ``docs/ISSUE_LEDGER.md`` keeps the ledger's front page."""
LEDGER_FRAGMENT_RE = re.compile(r"^I-(\d{3,})\.md$")


def ledger_fragments(root: Path | None = None) -> list[Path]:
    """The ledger's entry files, in id order."""
    folder = (root or ROOT) / LEDGER_DIR
    if not folder.is_dir():
        return []
    named = [(m, p) for p in folder.iterdir() if (m := LEDGER_FRAGMENT_RE.match(p.name))]
    return [p for m, p in sorted(named, key=lambda mp: int(mp[0].group(1)))]


def read_ledger(root: Path | None = None) -> str:
    """The whole ledger as one text: every entry file in id order, a blank line between
    them, the shape the parsers above read."""
    return "\n".join(p.read_text(encoding="utf-8") for p in ledger_fragments(root))


def ledger_fragment_problems(root: Path | None = None) -> list[str]:
    """Each file in ``docs/ledger/`` is one entry named by its id, and the old
    ``docs/ISSUE_LEDGER.md`` holds no entry: an entry written there would be read by no
    check (a branch that predates the fragments)."""
    root = root or ROOT
    folder = root / LEDGER_DIR
    rel = LEDGER_DIR.as_posix()
    errors: list[str] = []
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            if p.name != "README.md" and not LEDGER_FRAGMENT_RE.match(p.name):
                errors.append(f"{rel}/{p.name}: not an entry's name; an entry is {rel}/I-NNN.md")
    for p in ledger_fragments(root):
        text = p.read_text(encoding="utf-8")
        heads = [m.group(1) for m in LEDGER_ENTRY_RE.finditer(text)]
        if not text.startswith(f"### {p.stem} "):
            errors.append(f"{rel}/{p.name}: must start with its heading '### {p.stem} · <title>'")
        if len(heads) > 1:
            errors.append(f"{rel}/{p.name}: holds {len(heads)} entries; one file per entry")
    old = root / "docs" / "ISSUE_LEDGER.md"
    if old.is_file():
        errors += [
            f"docs/ISSUE_LEDGER.md: holds the entry {m.group(1)}; an entry is its own file, "
            f"{rel}/{m.group(1)}.md ({MIGRATE} moves it)"
            for m in LEDGER_ENTRY_RE.finditer(old.read_text(encoding="utf-8"))
        ]
    return errors


def check_ledger_statuses(root: Path | None = None) -> list[str]:
    """Every ledger entry says whether it is open or resolved (I-204), a closed one has no
    ``TODO`` left, and every entry is its own well-named file in ``docs/ledger/`` (W-38)."""
    problems = ledger_status_problems(read_ledger(root))
    return [f"{LEDGER_DIR.as_posix()}: {e}" for e in problems] + ledger_fragment_problems(root)


def guardrail_problems(
    rules: list[str], resolved: list[str], exists: Callable[[str, str], bool]
) -> list[str]:
    """Each rule starts with Always or Never and names guards that exist (``exists(kind,
    name)``); a ``manual:`` guard carries its reason after the token. Every resolved id is
    cited by some rule."""
    errors: list[str] = []
    cited: set[str] = set()
    for rule in rules:
        n = rule.split(".", 1)[0]
        if not GUARDRAIL_START_RE.match(rule):
            errors.append(f"guardrail {n}: must start with **Always** or **Never**")
        cited |= set(re.findall(r"\bI-\d{3}\b", rule))
        guard_text = rule.split("Guard:", 1)[1] if "Guard:" in rule else ""
        guards = list(GUARD_RE.finditer(guard_text))
        if not guards:
            errors.append(f"guardrail {n}: names no guard (symbol:, test:, check:, hook:, manual:)")
        for m in guards:
            kind, name = m.group(1), m.group(2).strip()
            if kind == "manual":
                if not guard_text[m.end() :].strip(" ,;"):
                    errors.append(f"guardrail {n}: a manual guard needs its reason after it")
            elif not exists(kind, name):
                errors.append(f"guardrail {n}: {kind}:{name} does not exist")
    for lid in resolved:
        if lid not in cited:
            errors.append(f"{lid} is resolved but no guardrail cites it; add an Always/Never rule")
    return errors


def guard_exists(kind: str, name: str, root: Path | None = None) -> bool:
    """Whether a named guard is in the repository: a symbol defined in the code, a test
    function under ``tests/``, a check that ``main`` runs, a hook in ``.claude/settings.json``."""
    root = root or ROOT
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return False
    if kind == "check":
        src = Path(__file__).resolve().read_text(encoding="utf-8")  # this file's main
        return f"def {name}(" in src and f"{name}()" in src.split("\ndef main(", 1)[-1]
    if kind == "hook":
        settings = root / ".claude" / "settings.json"
        return settings.exists() and name in settings.read_text(encoding="utf-8")
    if kind == "test":
        pattern = re.compile(rf"^\s*def {name}[\[(]", re.MULTILINE)  # [ : a generic (I-111)
        dirs: tuple[str, ...] = ("tests",)
    else:  # symbol
        pattern = re.compile(rf"^\s*(?:def {name}[\[(]|class {name}\b|{name}\s*[:=])", re.MULTILINE)
        dirs = CODE_DIRS
    return any(
        pattern.search(p.read_text(encoding="utf-8"))
        for d in dirs
        for p in (root / d).rglob("*.py")
        if "__pycache__" not in p.parts
    )


GUARDRAIL_DIR = Path("docs") / "guardrails"
"""The guardrails: one file per rule, ``NNN.md``, three digits (issue #122, W-39). The
``## Guardrails`` section of LESSONS_LEARNED.md says how the rules work and points here."""
GUARDRAIL_FRAGMENT_RE = re.compile(r"^(\d{3,})\.md$")


def guardrail_fragments(root: Path | None = None) -> list[Path]:
    """The guardrail files, in number order."""
    folder = (root or ROOT) / GUARDRAIL_DIR
    if not folder.is_dir():
        return []
    named = [(m, p) for p in folder.iterdir() if (m := GUARDRAIL_FRAGMENT_RE.match(p.name))]
    return [p for m, p in sorted(named, key=lambda mp: int(mp[0].group(1)))]


def guardrail_fragment_rules(root: Path | None = None) -> list[str]:
    """The rule lines of the guardrail files, in number order: what ``guardrail_rules``
    read from LESSONS_LEARNED.md before the fragments."""
    return [
        ln
        for p in guardrail_fragments(root)
        for ln in p.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def guardrail_fragment_problems(root: Path | None = None) -> list[str]:
    """Each file in ``docs/guardrails/`` is one rule line in a file named by its number, and
    the ``## Guardrails`` section of LESSONS_LEARNED.md holds no rule: a rule written there
    would be read by no check (a branch that predates the fragments)."""
    root = root or ROOT
    folder = root / GUARDRAIL_DIR
    rel = GUARDRAIL_DIR.as_posix()
    errors: list[str] = []
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            m = GUARDRAIL_FRAGMENT_RE.match(p.name)
            if p.name != "README.md" and (m is None or p.name != f"{int(m.group(1)):03d}.md"):
                errors.append(f"{rel}/{p.name}: not a rule's name; a rule is {rel}/NNN.md")
    for p in guardrail_fragments(root):
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        number = re.match(r"^(\d+)\. ", lines[0]) if len(lines) == 1 else None
        if len(lines) != 1:
            errors.append(f"{rel}/{p.name}: holds {len(lines)} lines; a rule is one line")
        elif number is None or int(number.group(1)) != int(p.stem):
            got = number.group(1) if number else "no number"
            errors.append(f"{rel}/{p.name}: holds rule {got}; its file name says {int(p.stem)}")
    lessons = root / "docs" / "LESSONS_LEARNED.md"
    if lessons.is_file():
        errors += [
            f"docs/LESSONS_LEARNED.md: rule {rule.split('.', 1)[0]} is written in ## Guardrails; "
            f"a rule is its own file, {rel}/NNN.md ({MIGRATE} moves it)"
            for rule in guardrail_rules(lessons.read_text(encoding="utf-8"))
        ]
    return errors


def check_guardrails(root: Path | None = None) -> list[str]:
    """The Always/Never rules are enforced, not just written (issue #61): every resolved
    ledger entry from I-104 on is cited by a rule, and every guard a rule names exists. The
    rules are read from ``docs/guardrails/`` and the entries from ``docs/ledger/`` (W-38,
    W-39)."""
    root = root or ROOT
    problems = guardrail_problems(
        guardrail_fragment_rules(root),
        resolved_ledger_ids(read_ledger(root)),
        lambda kind, name: guard_exists(kind, name, root),
    )
    return [f"{GUARDRAIL_DIR.as_posix()}: {e}" for e in problems] + guardrail_fragment_problems(
        root
    )


def check_workflow_checklist() -> list[str]:
    """The Workflow v2 checklist (issue #122) is well formed. Its status is computed from each
    item's evidence by ``scripts/workflow_status.py``, never ticked, so the only thing to
    check here is its shape: contiguous ids, a closer on every item, known evidence kinds,
    and a ``Done:`` line only on a manual item."""
    spec = importlib.util.spec_from_file_location(
        "workflow_status", ROOT / "scripts" / "workflow_status.py"
    )
    assert spec is not None and spec.loader is not None
    ws = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ws  # a dataclass resolves its module through sys.modules
    spec.loader.exec_module(ws)
    path = ROOT / "docs" / "WORKFLOW_V2.md"
    if not path.is_file():
        return ["docs/WORKFLOW_V2.md is missing (issue #122)"]
    return [
        f"docs/WORKFLOW_V2.md: {e}" for e in ws.problems(ws.parse(path.read_text(encoding="utf-8")))
    ]


def main() -> int:
    errors = (
        check_links()
        + check_model_ids()
        + check_changelog()
        + check_readme_status()
        + check_release_cadence()
        + check_released_section_frozen()
        + check_ignored_sources()
        + check_graph_sections()
        + check_guardrails()
        + check_scheduled_task_recipes()
        + check_ledger_statuses()
        + check_workflow_checklist()
    )
    for e in errors:
        print(f"docs-check: {e}")
    print(f"docs-check: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
