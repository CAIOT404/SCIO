"""`scripts/gate.py` (issue #88), the local CI-equivalent: it fails when a step fails, and its
regression evals are CI's (pre-flight 10: a chain that gates a push must be able to fail).

Gate v2 (issue #122, W-11 to W-16): the stamp and the RUNNING marker, the skip of a tree that
already passed, the docs-only and quick modes, the evals only when code changed, and the
machine lock. Those tests run the gate on real git repositories in ``tmp_path`` and fake the
steps' subprocesses."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from scio.settings import REPO_ROOT


@pytest.fixture(autouse=True)
def machine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The machine lock lives in a directory of this test's own. A test that took the real
    lock would wait forever inside the gate that runs it."""
    machine = tmp_path / "machine"
    monkeypatch.setenv("LOCALAPPDATA", str(machine))
    monkeypatch.setenv("XDG_CACHE_HOME", str(machine))
    return machine


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gate", REPO_ROOT / "scripts" / "gate.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gate"] = mod  # @dataclass looks its module up there (I-174)
    spec.loader.exec_module(mod)
    return mod


def _touch(path: Path) -> list[str]:
    """A step that passes and leaves ``path`` behind, so a test can see it ran."""
    return [sys.executable, "-c", f"open({str(path)!r}, 'w').close()"]


FAIL = [sys.executable, "-c", "raise SystemExit(3)"]


def test_a_failing_first_step_fails_the_gate_before_the_tests_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load()
    ran = tmp_path / "tests-ran"
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [("ruff check", FAIL)])
    monkeypatch.setattr(gate, "TOGETHER", [("pytest", _touch(ran))])
    monkeypatch.setattr(gate, "LAST", [])
    assert gate.main() == 1
    assert not ran.exists()
    out = capsys.readouterr().out
    assert "FAIL ruff check" in out and "exit 3" in out and "FAILED" not in out  # stopped early


def test_a_failing_test_step_fails_the_gate_and_the_docs_check_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load()
    evals_ran, docs_ran = tmp_path / "evals-ran", tmp_path / "docs-ran"
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [])
    monkeypatch.setattr(gate, "TOGETHER", [("pytest", FAIL), ("evals", _touch(evals_ran))])
    monkeypatch.setattr(gate, "LAST", [("docs", _touch(docs_ran))])
    assert gate.main() == 1
    assert evals_ran.exists() and docs_ran.exists()
    assert "gate: FAILED" in capsys.readouterr().out


def test_a_gate_whose_steps_all_pass_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load()
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [("ruff check", _touch(tmp_path / "a"))])
    monkeypatch.setattr(gate, "TOGETHER", [("pytest", _touch(tmp_path / "b"))])
    monkeypatch.setattr(gate, "LAST", [("docs", _touch(tmp_path / "c"))])
    assert gate.main() == 0
    assert "gate: passed" in capsys.readouterr().out


def test_a_gate_in_an_environment_without_cis_extras_stops_before_any_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I-205 (and I-156 before it): a fresh worktree's first `uv run` installed the default
    groups only, and the release branch's gate failed mypy on 20 imports nobody changed. The
    gate now names what is missing and the sync to run, before any step."""
    gate = _load()
    ran = tmp_path / "ruff-ran"
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\n'
        'web = ["no-such-distribution-i205[standard]>=1", "pytest>=8"]\n'
        '[dependency-groups]\ndev = ["mypy>=1.14", {include-group = "web"}]\n',
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "FIRST", [("ruff check", _touch(ran))])
    monkeypatch.setattr(gate, "TOGETHER", [])
    monkeypatch.setattr(gate, "LAST", [])
    assert gate.main() == 1
    assert not ran.exists()
    out = capsys.readouterr().out
    assert "no-such-distribution-i205" in out and "uv sync --frozen --all-extras" in out
    assert gate.missing_distributions(tmp_path / "pyproject.toml") == ["no-such-distribution-i205"]
    assert gate.missing_distributions(REPO_ROOT / "pyproject.toml") == []  # this env is CI's


def test_the_gates_regression_evals_are_cis() -> None:
    """The suite, provider, trials, threshold and trial concurrency are written in two places,
    `scripts/gate.py` and CI's `evals` job; CI adds only `--out` for its job summary."""
    evals = dict(_load().steps())["evals"]
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    line = next(ln for ln in ci.splitlines() if "evals.runner" in ln)
    words = line.split("run:", 1)[1].split()
    if "--out" in words:
        i = words.index("--out")
        del words[i : i + 2]
    assert words[words.index("--suite") :] == evals[evals.index("--suite") :], line


# --- Gate v2 (issue #122) ---------------------------------------------------------------------

IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *IDENTITY, *args], cwd=repo, check=True, capture_output=True)
    return out.stdout.decode("utf-8").strip()


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path: Path) -> Path:
    """A small SCIO-shaped repository: a module and a script, each with a test that names it,
    a module no test names, the conftest, two tests that read docs, and docs."""
    repo = tmp_path / "repo"
    files = {
        ".gitignore": ".scio/\n",
        "scio/__init__.py": "",
        "scio/x/__init__.py": "",
        "scio/x/y.py": "Y = 1\n",
        "scio/x/w.py": "W = 1\n",
        "scripts/z.py": "Z = 1\n",
        "tests/conftest.py": "",
        "tests/unit/test_y.py": "from scio.x import y\n",
        "tests/unit/test_z.py": 'SCRIPT = "scripts/z.py"\n',
        "tests/unit/test_check_docs.py": "",
        "tests/unit/test_guardrails_cited.py": "",
        "docs/guide.md": "# Guide\n",
        "README.md": "# Readme\n",
    }
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


Ran = list[tuple[str, list[str]]]


def _fake_steps(gate: ModuleType, monkeypatch: pytest.MonkeyPatch, fail: str = "") -> Ran:
    """Every step "runs" by being recorded; the one named ``fail`` exits 1."""
    ran: Ran = []

    def run(name: str, argv: list[str], logs: Path) -> object:
        ran.append((name, argv))
        log = logs / f"{name.replace(' ', '-')}.log"
        log.write_text("1 passed in 0.01s\n", encoding="utf-8")
        return gate.Step(name, 1 if name == fail else 0, 0.0, log)

    monkeypatch.setattr(gate, "_run", run)
    return ran


def _tests_in(ran: Ran) -> list[str] | None:
    """The test paths the pytest step named: [] for the whole suite, None when it did not run."""
    argv = dict(ran).get("pytest")
    return None if argv is None else argv[argv.index("-n") + 2 :]


def _stamp(repo: Path) -> dict[str, object]:
    data = json.loads((repo / ".scio" / "gate" / "last-pass.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _scenario(
    gate: ModuleType,
    repo: Path,
    ran: Ran,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, str],
    argv: Sequence[str] = (),
) -> str:
    """Reset ``repo`` to HEAD, stamp HEAD's tree as a quick pass, make ``change``, gate it."""
    _git(repo, "checkout", "-q", "--", ".")
    _git(repo, "clean", "-fdq")
    tree, head = _git(repo, "rev-parse", "HEAD^{tree}"), _git(repo, "rev-parse", "HEAD")
    gate.write_stamp(tree, head, "quick", {})
    for rel, text in change.items():
        _write(repo, rel, text)
    ran.clear()
    capsys.readouterr()
    assert gate.main(argv) == 0
    return capsys.readouterr().out


DRIVER = """
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("gate", sys.argv[1])
gate = importlib.util.module_from_spec(spec)
sys.modules["gate"] = gate
spec.loader.exec_module(gate)
gate.ROOT = Path(sys.argv[2])
KILL = "import json, os, signal, sys; os.kill(json.load(open(sys.argv[1]))['pid'], signal.SIGTERM)"
gate.FIRST = [("ruff check", [sys.executable, "-c", KILL, str(gate.running_path())])]
gate.TOGETHER = []
gate.LAST = []
sys.exit(gate.main(["--no-lock", "--full"]))
"""
"""A gate whose first step kills the gate itself, as the 12:07 gate died (W-11): TerminateProcess
on Windows, an unhandled SIGTERM elsewhere. Neither runs a ``finally``."""


def test_a_pass_writes_the_stamp_and_a_dead_gate_leaves_its_running_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-11: a pass writes `.scio/gate/last-pass.json` (the tested tree, HEAD, the mode, the
    time, each step's verdict) with LF through a rename; a failed gate leaves the last stamp as
    it was; `RUNNING` exists while a gate runs, so a gate killed hard leaves it behind."""
    gate = _load()
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate, "ROOT", repo)
    _fake_steps(gate, monkeypatch)
    assert gate.main(["--full"]) == 0
    path = repo / ".scio" / "gate" / "last-pass.json"
    assert b"\r" not in path.read_bytes()
    stamp = _stamp(repo)
    assert stamp["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert stamp["head"] == _git(repo, "rev-parse", "HEAD") and stamp["mode"] == "full"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(stamp["at"]))
    counts = stamp["counts"]
    assert isinstance(counts, dict)
    assert set(counts) == {"ruff check", "ruff format", "mypy", "pytest", "evals", "docs"}
    assert counts["pytest"] == "1 passed in 0.01s"
    gate_dir = repo / ".scio" / "gate"
    assert not (gate_dir / "RUNNING").exists() and not list(gate_dir.glob("*.tmp"))

    # Gated before the commit: the stamp holds the tree the commit then has.
    _write(repo, "scio/x/y.py", "Y = 2\n")
    _write(repo, "scio/x/new.py", "N = 1\n")
    capsys.readouterr()
    assert gate.main(["--full"]) == 0
    assert "the stamped tree holds 1 untracked file(s) (scio/x/new.py)" in capsys.readouterr().out
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "y")
    assert _stamp(repo)["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")

    # A failed gate writes no stamp.
    before = path.read_bytes()
    _write(repo, "scio/x/y.py", "Y = 3\n")
    _fake_steps(gate, monkeypatch, fail="pytest")
    assert gate.main(["--full"]) == 1
    assert path.read_bytes() == before and not (gate_dir / "RUNNING").exists()

    # A gate killed hard leaves RUNNING behind, and no stamp.
    driver, dead = tmp_path / "driver.py", tmp_path / "dead"
    driver.write_text(DRIVER, encoding="utf-8", newline="\n")
    dead.mkdir()
    script = REPO_ROOT / "scripts" / "gate.py"
    r = subprocess.run(
        [sys.executable, str(driver), str(script), str(dead)], capture_output=True, timeout=120
    )
    marker = dead / ".scio" / "gate" / "RUNNING"
    assert r.returncode != 0 and marker.is_file(), r.stderr.decode(errors="replace")
    left = json.loads(marker.read_text(encoding="utf-8"))
    assert left["pid"] != os.getpid() and not gate.pid_alive(left["pid"])
    assert not (dead / ".scio" / "gate" / "last-pass.json").exists()

    # The next gate there says the last one died, and where its logs are.
    monkeypatch.setattr(gate, "ROOT", dead)
    _fake_steps(gate, monkeypatch)
    capsys.readouterr()
    assert gate.main(["--full"]) == 0
    out = capsys.readouterr().out
    assert f"(pid {left['pid']}) died before it finished" in out and not marker.exists()


def test_a_tree_that_already_passed_is_not_gated_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-12: the stamp's tree is HEAD's tree and the worktree is clean, so the gate exits 0 at
    once; a merge commit with the same content is the same tree. `--full` still runs over a
    quick pass, and any change is gated."""
    gate = _load()
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate, "ROOT", repo)
    ran = _fake_steps(gate, monkeypatch)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    gate.write_stamp(tree, _git(repo, "rev-parse", "HEAD"), "quick", {})
    assert gate.main([]) == 0 and ran == []
    assert f"gate: tree {tree[:12]} already passed (quick, " in capsys.readouterr().out
    assert sorted(p.name for p in (repo / ".scio" / "gate").iterdir()) == ["last-pass.json"]

    _git(repo, "commit", "-q", "--allow-empty", "-m", "Merge branch 'main'")
    assert gate.main([]) == 0 and ran == []

    assert gate.main(["--full"]) == 0 and {n for n, _ in ran} >= {"pytest", "evals"}
    ran.clear()
    assert gate.main(["--full"]) == 0 and ran == []  # now it passed full

    _write(repo, "scripts/z.py", "Z = 2\n")
    assert gate.main([]) == 0 and ran


def test_a_docs_only_change_since_a_gated_tree_runs_only_the_docs_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-13: every path changed since the stamp is Markdown documentation, so only the docs
    check and the tests that read docs run, and the stamp says `docs`. A prompt template is
    Markdown the code reads, so it is code; with no stamp at all the gate runs quick."""
    gate = _load()
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate, "ROOT", repo)
    ran = _fake_steps(gate, monkeypatch)
    out = _scenario(gate, repo, ran, capsys, {"docs/guide.md": "# Guide\n\nMore.\n", "NEW.md": ""})
    assert sorted(n for n, _ in ran) == ["docs", "pytest"]
    assert _tests_in(ran) == [
        "tests/unit/test_check_docs.py",
        "tests/unit/test_guardrails_cited.py",
    ]
    assert "gate: docs: 2 changed path(s) since the stamp's tree" in out
    stamp = _stamp(repo)
    assert stamp["mode"] == "docs" and stamp["tree"] == gate.tested_tree()

    template = "scio/prompt/templates/coder/v1.md"
    _scenario(gate, repo, ran, capsys, {"docs/guide.md": "# Guide\n\nMore.\n", template: "x\n"})
    assert "mypy" in dict(ran) and _stamp(repo)["mode"] == "quick"

    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _scenario(gate, repo, ran, capsys, {"docs/guide.md": "# Guide\n\nAgain.\n"})
    (repo / ".scio" / "gate" / "last-pass.json").unlink()
    ran.clear()
    assert gate.main([]) == 0
    assert "mypy" in dict(ran) and _stamp(repo)["mode"] == "quick"


def test_the_quick_gate_runs_the_tests_of_the_changed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-14: by default the gate runs lint and types, then the tests of the paths changed since
    the stamp's tree (or since origin/main without one), then the docs check. A shared fixture,
    a config, or a source no test names runs the whole suite, and the gate says which."""
    gate = _load()
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate, "ROOT", repo)
    ran = _fake_steps(gate, monkeypatch)

    def gated(change: dict[str, str]) -> tuple[list[str] | None, str]:
        out = _scenario(gate, repo, ran, capsys, change)
        names = [n for n, _ in ran]
        assert names[:3] == ["ruff check", "ruff format", "mypy"] and names[-1] == "docs", names
        assert _stamp(repo)["mode"] == "quick"
        return _tests_in(ran), out

    files, out = gated({"scio/x/y.py": "Y = 2\n"})
    assert files == ["tests/unit/test_y.py"]
    assert "tests: 1 file(s) for the changed paths: tests/unit/test_y.py" in out
    assert gated({"scripts/z.py": "Z = 2\n"})[0] == ["tests/unit/test_z.py"]
    assert gated({"tests/unit/test_z.py": "Z = 'scripts/z.py'\n"})[0] == ["tests/unit/test_z.py"]
    files, _ = gated({"scripts/z.py": "Z = 2\n", "docs/guide.md": "# Guide\n\nMore.\n"})
    assert files == [
        "tests/unit/test_check_docs.py",
        "tests/unit/test_guardrails_cited.py",
        "tests/unit/test_z.py",
    ]
    for change, why in [
        ({"tests/conftest.py": "X = 1\n"}, "tests/conftest.py changed"),
        ({"configs/models.yaml": "a: 1\n"}, "configs/models.yaml changed"),
        ({"scio/x/w.py": "W = 2\n"}, "scio/x/w.py maps to no test"),
    ]:
        files, out = gated(change)
        assert files == [] and dict(ran)["pytest"] == gate.TOGETHER[0][1], change
        assert f"tests: the whole suite ({why})" in out

    # No stamp: the paths changed since the merge base with origin/main.
    _git(repo, "checkout", "-q", "--", ".")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feat/z")
    _write(repo, "scripts/z.py", "Z = 3\n")
    _git(repo, "commit", "-q", "-am", "z")
    (repo / ".scio" / "gate" / "last-pass.json").unlink()
    ran.clear()
    capsys.readouterr()
    assert gate.main([]) == 0
    assert _tests_in(ran) == ["tests/unit/test_z.py"]
    assert "since origin/main" in capsys.readouterr().out


def test_the_gate_skips_the_evals_when_no_code_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-15: the regression evals run when `scio/`, `evals/`, `configs/` or `prompts/` changed
    since the stamp's tree, or with `--full`; otherwise the gate says it skipped them."""
    gate = _load()
    repo = _repo(tmp_path)
    monkeypatch.setattr(gate, "ROOT", repo)
    ran = _fake_steps(gate, monkeypatch)
    for change in (
        {"scripts/z.py": "Z = 2\n"},
        {"tests/unit/test_y.py": "from scio.x import y\n#\n"},
    ):
        out = _scenario(gate, repo, ran, capsys, change)
        assert "evals" not in dict(ran) and "evals: skipped (no code changed)" in out
        counts = _stamp(repo)["counts"]
        assert isinstance(counts, dict) and counts["evals"] == "skipped (no code changed)"
    for change in (
        {"scio/x/y.py": "Y = 2\n"},
        {"evals/new_task.py": "T = 1\n"},
        {"configs/budgets.yaml": "a: 1\n"},
        {"prompts/system.md": "Be brief.\n"},
    ):
        out = _scenario(gate, repo, ran, capsys, change)
        assert dict(ran)["evals"] == gate.TOGETHER[1][1] and "evals: skipped" not in out, change
    _scenario(gate, repo, ran, capsys, {"scripts/z.py": "Z = 2\n"}, ["--full"])
    assert "evals" in dict(ran)


def _unlink(path: Path) -> None:
    """Windows refuses to delete a file another thread has open for a moment."""
    for _ in range(50):
        try:
            path.unlink()
            return
        except PermissionError:
            time.sleep(0.02)
    path.unlink()


def _in_thread(call: Callable[[], int]) -> tuple[threading.Thread, dict[str, int]]:
    result: dict[str, int] = {}
    thread = threading.Thread(target=lambda: result.update(code=call()), daemon=True)
    thread.start()
    return thread, result


def test_a_second_gate_waits_for_the_machine_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    machine_dir: Path,
) -> None:
    """W-16: one gate at a time on the machine. A second gate says whose gate it waits for,
    once, and starts when the lock is released; a lock whose process is gone is taken over;
    `--no-lock` never touches it."""
    gate = _load()
    monkeypatch.setattr(gate, "LOCK_POLL_S", 0.05)
    lock = gate.lock_path()
    assert lock.is_relative_to(machine_dir) and lock.name == "gate.lock"
    lock.parent.mkdir(parents=True)
    holder = {"pid": os.getpid(), "worktree": "C:/elsewhere/wt", "at": "2026-11-14T12:00:00Z"}
    lock.write_text(json.dumps(holder), encoding="utf-8")
    plain, ran = tmp_path / "plain", tmp_path / "ruff-ran"
    plain.mkdir()
    monkeypatch.setattr(gate, "ROOT", plain)
    monkeypatch.setattr(gate, "FIRST", [("ruff check", _touch(ran))])
    monkeypatch.setattr(gate, "TOGETHER", [])
    monkeypatch.setattr(gate, "LAST", [])

    thread, result = _in_thread(lambda: gate.main([]))
    time.sleep(0.6)
    assert thread.is_alive() and not ran.exists()
    out = capsys.readouterr().out
    waiting = f"gate: waiting for the gate in C:/elsewhere/wt (pid {os.getpid()}, since "
    assert out.count(waiting) == 1, out
    _unlink(lock)
    thread.join(timeout=60)
    assert not thread.is_alive() and result == {"code": 0} and ran.exists()
    assert not lock.exists()  # released

    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    lock.write_text(json.dumps({**holder, "pid": finished.pid}), encoding="utf-8")
    assert gate.acquire_lock(lock, "C:/here") is False  # taken over, without waiting
    assert json.loads(lock.read_text(encoding="utf-8"))["worktree"] == "C:/here"
    assert "gate: taking over a stale lock" in capsys.readouterr().out
    gate.release_lock(lock)
    assert not lock.exists()

    lock.write_text(json.dumps(holder), encoding="utf-8")
    ran.unlink()
    assert gate.main(["--no-lock"]) == 0 and ran.exists()
    assert json.loads(lock.read_text(encoding="utf-8")) == holder
