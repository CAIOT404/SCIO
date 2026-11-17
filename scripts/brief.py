"""Fill the agent brief template from a GitHub issue (issue #122, W-33).

Usage: ``uv run python scripts/brief.py <issue> [--out FILE] [--worktree DIR] [--branch B]``

1. Reads the issue with ``gh issue view N --json number,title,body,labels``.
2. Extracts the file paths it names (a bare file name is resolved when it is unique in the
   tree) and the symbols it names in backticks (``snake_case``, ``CamelCase`` or dotted).
3. Proposes the owned files: the named paths, the files that define a named symbol, and the
   tests that import an owned module or use a named symbol.
4. Finds the docs touchpoints of those paths and symbols: ``docs/ARCHITECTURE.md``,
   ``docs/adr/*.md``, the comments in ``configs/*.yaml``, ``scio/dashboard/static/*`` and
   ``README.md`` lines.
5. Fills ``.claude/templates/agent-brief.md``. The decided design stays a ``TODO``, and so
   does each invariant's test: one invariant slot per sentence the issue quotes from the owner
   (a double-quoted span of three words or more, straight or curly quotes, on a line that
   names the owner, or a blockquote that follows such a line).
6. Fills the Pins edge decision: every test file outside the owned ones that imports an owned
   module or names an owned symbol, with the reason, and adds them to the checks command.
   The Identity, Failure and Resume decisions stay a ``TODO`` for the orchestrator.
7. Writes the brief to stdout, or to ``--out``.

``uv run python scripts/brief.py --check FILE`` is the gate before sending: it exits 1 and
names each ``TODO`` left in the brief (line number and section), and exits 0 when there is
none. It reads only the file: no ``gh``, no ``git``.

The proposal is a starting point for the orchestrator, who reviews it before sending: a grep
finds text, not intent.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(".claude") / "templates" / "agent-brief.md"
Run = Callable[[list[str]], str]
"""``run(argv) -> stdout``; raises on a failed command. Tests replace it."""

SHARED_FILES = (
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "docs/ISSUE_LEDGER.md",
    "docs/LESSONS_LEARNED.md",
    "CHANGELOG.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".claude/settings.json",
)
"""The orchestrator's files (CLAUDE.md, the orchestration rules): never an agent's."""
CODE_DIRS = ("scio", "scripts", "recovery", "evals")
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".scio", ".mypy_cache",
              ".ruff_cache", ".pytest_cache", "worktrees"}  # fmt: skip
EXTENSIONS = "py|md|yaml|yml|json|toml|js|ts|tsx|html|css|sh|txt|cfg|ini"
PATH_RE = re.compile(rf"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:{EXTENSIONS}))(?![\w/-])")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
IDENT_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
QUOTE_RE = re.compile(r"\"([^\"\n]+)\"|“([^”\n]+)”")
OWNER_RE = re.compile(r"\bowner\b", re.IGNORECASE)
PER_FILE = 6
"""Docs touchpoint lines kept per file; the rest are counted."""
MAX_TESTS = 10


def default_run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def fetch_issue(number: int, run: Run) -> dict[str, Any]:
    """The issue's number, title, body and labels, from ``gh``."""
    out = run(["gh", "issue", "view", str(number), "--json", "number,title,body,labels"])
    data: dict[str, Any] = json.loads(out)
    return data


# ---------- what the issue names ----------


def _outside_fences(body: str) -> Iterable[str]:
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        yield line


def named_paths(body: str) -> list[str]:
    """The file paths the body names, in order, without duplicates."""
    return list(dict.fromkeys(m.group(1).removeprefix("./") for m in PATH_RE.finditer(body)))


def named_symbols(body: str) -> list[str]:
    """Identifiers the body names in backticks: dotted, ``snake_case`` or ``CamelCase``."""
    found: list[str] = []
    for m in BACKTICK_RE.finditer(body):
        text = m.group(1).strip().removesuffix("()")
        if not IDENT_RE.match(text) or PATH_RE.fullmatch(text):
            continue
        for part in text.split("."):
            camel = part[:1].isupper() and any(c.islower() for c in part)
            if len(part) >= 4 and ("_" in part.strip("_") or camel):
                found.append(part)
    return list(dict.fromkeys(found))


def owner_quotes(body: str) -> list[str]:
    """The sentences the body quotes from the owner: double-quoted spans of three words or
    more on a line that names the owner, and blockquotes that follow such a line."""
    quotes: list[str] = []
    previous = ""
    block: list[str] = []
    for line in [*_outside_fences(body), ""]:
        stripped = line.strip()
        if stripped.startswith(">"):
            block.append(stripped.lstrip(">").strip())
            continue
        if block:
            text = " ".join(b for b in block if b)
            if OWNER_RE.search(previous) or OWNER_RE.search(text):
                quotes.extend(_quoted(text) or [text.strip('"“” ')])
            block = []
        if OWNER_RE.search(stripped):
            quotes.extend(_quoted(stripped))
        if stripped:
            previous = stripped
    return list(dict.fromkeys(q for q in quotes if len(q.split()) >= 3))


def _quoted(text: str) -> list[str]:
    return [(m.group(1) or m.group(2)).strip() for m in QUOTE_RE.finditer(text)]


# ---------- the tree ----------


def list_files(root: Path, run: Run) -> list[str]:
    """Tracked files (``git ls-files``), or a walk when ``root`` is not a git checkout."""
    try:
        out = run(["git", "-C", str(root), "ls-files"])
    except (OSError, subprocess.CalledProcessError):
        out = ""
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if files:
        return files
    walked: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
        rel = Path(dirpath).relative_to(root)
        walked.extend((rel / f).as_posix() for f in filenames)
    return sorted(walked)


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _module(rel: str) -> str:
    return rel.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _is_code(rel: str) -> bool:
    return (
        rel.endswith(".py")
        and rel.split("/", 1)[0] in CODE_DIRS
        and not rel.startswith("evals/tasks/")
    )


def top_level_names(source: str) -> list[str]:
    """The module-level functions, classes and assignments of a Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _distinctive(name: str) -> bool:
    camel = name[:1].isupper() and any(c.islower() for c in name)
    return not name.startswith("_") and len(name) >= 5 and ("_" in name or camel)


def _term_re(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.]){re.escape(term)}(?!\w)")


@dataclass
class Proposal:
    """What :func:`propose` found: owned files, docs touchpoints and the search terms."""

    paths: dict[str, bool] = field(default_factory=dict)
    """Each named path, and whether it exists."""
    code: dict[str, str] = field(default_factory=dict)
    """Owned code file -> why."""
    doc_files: dict[str, str] = field(default_factory=dict)
    """Named Markdown files that are not the orchestrator's -> why."""
    foreign: list[str] = field(default_factory=list)
    """Named paths outside this tree's directories: evidence, never owned."""
    tests: dict[str, str] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)
    """Test file outside the owned ones that pins the owned code -> why."""
    docs: list[str] = field(default_factory=list)
    """Docs touchpoint lines, ready for the brief."""
    terms: list[str] = field(default_factory=list)


def propose(body: str, root: Path, files: list[str]) -> Proposal:
    """Owned files and docs touchpoints for the paths and symbols ``body`` names."""
    prop = Proposal()
    by_name: dict[str, list[str]] = {}
    for rel in files:
        by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    known = set(files)
    top_dirs = {f.split("/", 1)[0] for f in files if "/" in f}
    for path in named_paths(body):
        if path in known:
            prop.paths[path] = True
        elif "/" not in path and len(by_name.get(path, [])) == 1:
            prop.paths[by_name[path][0]] = True  # a bare file name, unique in the tree
        elif (root / path).exists() or path.split("/", 1)[0] in top_dirs:
            prop.paths[path] = (root / path).exists()  # a new file in one of our directories
        else:
            prop.foreign.append(path)  # another repository's, or a run's artifact
    symbols = named_symbols(body)
    for path, exists in prop.paths.items():
        why = "named in the issue" if exists else "new: named in the issue"
        if path in SHARED_FILES or path.startswith("tests/"):
            continue
        if path.endswith(".md"):
            prop.doc_files[path] = why
        else:
            prop.code[path] = why
    code_files = [f for f in files if _is_code(f)]
    defined: dict[str, list[str]] = {}
    for rel in code_files:
        for name in top_level_names(_read(root, rel)):
            defined.setdefault(name, []).append(rel)
    for sym in symbols:
        for rel in defined.get(sym, []):
            prop.code.setdefault(rel, f"defines `{sym}`")
    for path, exists in prop.paths.items():
        if path.startswith("tests/") and exists:
            prop.tests[path] = "named in the issue"
    terms = list(symbols)
    modules: list[str] = []
    public: list[str] = []
    for rel in prop.code:
        if not rel.endswith(".py"):
            continue
        modules.append(_module(rel))
        terms += [rel, rel.rsplit("/", 1)[-1], _module(rel)]
        if prop.paths.get(rel):  # a named module: its public names are its surface
            public += [n for n in top_level_names(_read(root, rel)) if _distinctive(n)]
    prop.terms = list(dict.fromkeys(terms + public))
    _find_tests(prop, root, files, symbols, modules, public)
    prop.docs = docs_touchpoints(root, files, prop.terms)
    return prop


def _find_tests(
    prop: Proposal,
    root: Path,
    files: list[str],
    symbols: list[str],
    modules: list[str],
    public: list[str],
) -> None:
    """The owned tests (the ``MAX_TESTS`` that most import an owned module or use a symbol
    the issue names) and the pins: every other test file that imports an owned module or
    names an owned symbol (a named symbol, or a public name of a named module)."""
    import_res = [
        (m, re.compile(rf"\b(?:from|import)\s+{re.escape(m)}\b")) for m in modules
    ] + [
        (m, re.compile(rf"\bfrom\s+{re.escape(m.rsplit('.', 1)[0])}\s+import\s+[^\n]*\b"
                       rf"{re.escape(m.rsplit('.', 1)[-1])}\b"))
        for m in modules if "." in m
    ]  # fmt: skip
    named = set(symbols)
    symbol_res = [(s, _term_re(s)) for s in dict.fromkeys([*symbols, *public])]
    scored: list[tuple[int, str]] = []
    why: dict[str, str] = {}
    for rel in files:
        if not (rel.startswith("tests/") and rel.endswith(".py")) or rel in prop.tests:
            continue
        text = _read(root, rel)
        imported = [m for m, r in import_res if r.search(text)]
        used = [s for s, r in symbol_res if r.search(text)]
        score = 2 * len(imported) + sum(1 for s in used if s in named)
        if score:
            scored.append((score, rel))
        reasons = [
            f"{verb} {', '.join(f'`{n}`' for n in dict.fromkeys(names))}"
            for verb, names in (("imports", imported), ("names", used))
            if names
        ]
        if reasons:
            why[rel] = "; ".join(reasons)
    for _, rel in sorted(scored, key=lambda s: (-s[0], s[1]))[:MAX_TESTS]:
        prop.tests[rel] = "imports an owned module or uses a named symbol"
    prop.pins = {rel: why[rel] for rel in sorted(why) if rel not in prop.tests}


def _docs_files(files: list[str]) -> list[str]:
    def wanted(rel: str) -> bool:
        return (
            rel in ("docs/ARCHITECTURE.md", "README.md")
            or (rel.startswith("docs/adr/") and rel.endswith(".md"))
            or (rel.startswith("configs/") and rel.endswith((".yaml", ".yml")))
            or rel.startswith("scio/dashboard/static/")
        )

    return [f for f in files if wanted(f)]


def docs_touchpoints(root: Path, files: list[str], terms: list[str]) -> list[str]:
    """``path:line`` lines in the docs that name one of ``terms``. In ``configs/*.yaml``
    only comments count: the values are data, not prose."""
    patterns = [(t, _term_re(t)) for t in terms]
    out: list[str] = []
    for rel in _docs_files(files):
        hits: list[str] = []
        shared = " (shared: report it under `docs_touchpoints_left`)" if rel in SHARED_FILES else ""
        for i, line in enumerate(_read(root, rel).splitlines(), 1):
            text = line
            if rel.startswith("configs/"):
                if "#" not in line:
                    continue
                text = line[line.index("#") :]
            term = next((t for t, r in patterns if r.search(text)), None)
            if term is not None:
                hits.append(f"- `{rel}:{i}` (`{term}`){shared}: {line.strip()[:160]}")
        out.extend(hits[:PER_FILE])
        if len(hits) > PER_FILE:
            out.append(f"- `{rel}`: {len(hits) - PER_FILE} more lines name these terms")
    return out


# ---------- the brief ----------


def _section(body: str, heading: str) -> str:
    lines = body.splitlines()
    head = re.compile(rf"^#{{1,6}}\s*{heading}\b", re.IGNORECASE)
    for i, line in enumerate(lines):
        if head.match(line.strip()):
            rest: list[str] = []
            for nxt in lines[i + 1 :]:
                if nxt.lstrip().startswith("#"):
                    break
                rest.append(nxt)
            return "\n".join(rest).strip()
    return ""


def _first_paragraph(body: str) -> str:
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if para and not para.startswith("#"):
            return para
    return ""


def _demote(body: str, levels: int = 2) -> str:
    """Headings moved ``levels`` deeper, so the issue sits under the brief's own section."""
    out: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"^#{1,6}\s", line):
            hashes = len(line) - len(line.lstrip("#"))
            line = "#" * min(hashes + levels, 6) + line[hashes:]
        out.append(line)
    return "\n".join(out)


def _slug(title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    return "-".join(words[:4]) or "work"


def _bullets(items: dict[str, str], empty: str) -> str:
    return "\n".join(f"- `{k}` ({v})" for k, v in items.items()) or empty


def invariant_slots(quotes: list[str]) -> str:
    """One slot per owner quote; the test name is the orchestrator's to fill."""
    if not quotes:
        return (
            "TODO: the issue quotes no owner sentence. State each invariant in the owner's "
            "words, quoted, with its test."
        )
    return "\n".join(
        f"{i}. > “{q}”\n   - test: `TODO test_...` in `TODO tests/...`; it fails on the old code."
        for i, q in enumerate(quotes, 1)
    )


def render(
    issue: dict[str, Any],
    prop: Proposal,
    template: str,
    *,
    worktree: str,
    branch: str,
    base: str,
    base_sha: str,
    parallel: str,
) -> str:
    """The template with every slot filled; TODO where only the orchestrator can decide."""
    number = str(issue.get("number", "?"))
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    labels = ", ".join(str(lb.get("name")) for lb in issue.get("labels") or [])
    goal = _section(body, "Goal") or _first_paragraph(body) or "TODO: the goal"
    if labels:
        goal += f"\n\nLabels: {labels}."
    path_lines = "\n".join(
        [
            *(f"- `{p}` ({'exists' if ok else 'new'})" for p, ok in prop.paths.items()),
            *(f"- `{p}` (not in this repository)" for p in prop.foreign),
        ]
    )
    evidence = f"Issue #{number}, verbatim:\n\n{_demote(body).strip()}"
    if path_lines:
        evidence += f"\n\nThe paths the issue names:\n{path_lines}"
    read_first = [f"- `{p}`" for p, ok in prop.paths.items() if ok]
    read_first += [f"- `{p}`" for p in prop.code if p not in prop.paths]
    tests = list(prop.tests)
    slots = {
        "issue": number,
        "title": title,
        "goal": goal,
        "worktree": worktree,
        "branch": branch,
        "base": base,
        "base_sha": base_sha,
        "parallel_agents": parallel,
        "read_first": "\n".join(dict.fromkeys(read_first)),
        "evidence": evidence,
        "decided_design": "TODO: the design the orchestrator decided: names, shapes, where "
        "each piece lives, and what stays unchanged.",
        "invariants": invariant_slots(owner_quotes(body)),
        "owned_code": _bullets(prop.code, "TODO: no code file found; name the owned files."),
        "owned_docs": "\n".join(
            [*(f"- `{k}` ({v})" for k, v in prop.doc_files.items()), *prop.docs]
        )
        or "none found (check by hand)",
        "owned_tests": _bullets(prop.tests, "TODO: the new test file")
        + "\n- `TODO tests/unit/test_<name>.py` (new: the invariant tests)",
        "pins": "\n".join(f"  - `{k}` ({v})" for k, v in prop.pins.items())
        or "  - none found: no test outside the owned files imports an owned module or names "
        "an owned symbol",
        "test_files": " ".join([*tests, *prop.pins, "TODO"]),
    }
    # The template's own comment is for whoever fills it by hand.
    text = re.sub(r"\A<!--.*?-->\n", "", template, flags=re.DOTALL)
    for key, value in slots.items():
        text = text.replace("{{" + key + "}}", value)
    note = (
        f"<!-- Filled by scripts/brief.py from issue #{number}. Review the proposed files and "
        "fill every TODO before sending. -->\n"
    )
    return note + text


# ---------- the check ----------

TODO_RE = re.compile(r"\bTODO\b")
COMMENT_RE = re.compile(r"<!--.*?-->")
EDGE_SECTION = "Edge decisions (decided before sending)"
EDGE_SLOTS = ("Identity", "Failure", "Resume", "Pins")


def remaining_todos(text: str) -> list[tuple[int, str, str]]:
    """Each ``TODO`` left in a brief: its line number, its ``##`` section and the line. An HTML
    comment is not the brief (the note this script writes at the top names the word)."""
    found: list[tuple[int, str, str]] = []
    section = "top"
    fenced = commented = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw
        if commented:
            if "-->" not in line:
                continue
            line = line.split("-->", 1)[1]
            commented = False
        line = COMMENT_RE.sub("", line)
        if "<!--" in line:
            line = line.split("<!--", 1)[0]
            commented = True
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            section = line[3:].strip()
        if TODO_RE.search(line):
            found.append((number, section, raw.strip()))
    return found


def missing_edge_decisions(text: str) -> list[str]:
    """The edge decisions a brief does not carry: a brief cut down by hand still states all four."""
    section = text.split(f"## {EDGE_SECTION}", 1)[1].split("\n## ", 1)[0] if (
        f"## {EDGE_SECTION}" in text
    ) else ""  # fmt: skip
    return [name for name in EDGE_SLOTS if f"- **{name}**" not in section]


def check(file: str) -> int:
    """1 while the brief at ``file`` has a ``TODO`` or lacks an edge decision, naming each;
    0 when it is ready to send. Reads only the file."""
    try:
        text = Path(file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"brief.py --check: cannot read the brief: {exc}", file=sys.stderr)
        return 1
    todos = remaining_todos(text)
    missing = missing_edge_decisions(text)
    for number, section, line in todos:
        print(f"{file}:{number}: [{section}] {line[:160]}")
    for name in missing:
        print(f"{file}: [{EDGE_SECTION}] has no `- **{name}**` line")
    if todos or missing:
        print(
            f"not ready to send: {len(todos)} TODO left, {len(missing)} edge decisions missing",
            file=sys.stderr,
        )
        return 1
    print("ready to send: no TODO left, and the four edge decisions are stated")
    return 0


def main(argv: list[str] | None = None, *, run: Run | None = None, root: Path | None = None) -> int:
    """Print (or write) the brief for one issue. ``run`` and ``root`` replace ``gh``/``git``
    and the repository in tests."""
    p = argparse.ArgumentParser(description="Fill the agent brief from a GitHub issue.")
    p.add_argument("issue", type=int, nargs="?", default=None)
    p.add_argument(
        "--check",
        metavar="FILE",
        default=None,
        help="exit 1 and name each TODO left in the brief FILE; reads no issue",
    )
    p.add_argument("--out", default=None, help="write here instead of stdout")
    p.add_argument("--worktree", default=None, help="default: TODO")
    p.add_argument("--branch", default=None, help="default: feat/<issue>-<slug>")
    p.add_argument("--base", default="main")
    p.add_argument("--parallel", default=None, help="the other agents of the wave")
    args = p.parse_args(argv)
    if args.check is not None:
        return check(args.check)
    if args.issue is None:
        p.error("an issue number, or --check FILE")
    run = run or default_run
    root = root or ROOT
    issue = fetch_issue(args.issue, run)
    files = list_files(root, run)
    prop = propose(str(issue.get("body") or ""), root, files)
    try:
        base_sha = run(["git", "-C", str(root), "rev-parse", "--short", args.base]).strip()
    except (OSError, subprocess.CalledProcessError):
        base_sha = ""
    text = render(
        issue,
        prop,
        (ROOT / TEMPLATE).read_text(encoding="utf-8"),
        worktree=args.worktree or "TODO: the worktree path",
        branch=args.branch or f"feat/{args.issue}-{_slug(str(issue.get('title') or ''))}",
        base=args.base,
        base_sha=base_sha or "TODO",
        parallel=args.parallel
        or "TODO: the other agents of this wave and their worktrees, or none",
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # an issue's text is not cp1252
    sys.exit(main())
