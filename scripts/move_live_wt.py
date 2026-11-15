"""Move ``.scio/live-wt`` to ``origin/main`` when no supervised build runs (issue #122, W-27).

    uv run python scripts/move_live_wt.py                  # move now if idle, else exit 3
    uv run python scripts/move_live_wt.py --when-idle      # wait until idle, then move
    uv run python scripts/move_live_wt.py --when-idle --max-wait 3600

live-wt is the checkout every SCIO build and the user-level push-gate hook run from, so a
merge to runtime code is not live until it moves; and moving it under a running build would
change the code beneath it. "Idle" is read from the recovery supervisor:
``python -m recovery status``, run from ``.scio/recovery-wt``, shows no run whose status is
``running`` and no open incident. A status that cannot be read counts as busy, never idle.

When idle: ``git -C .scio/live-wt fetch origin``, then ``checkout --detach origin/main``, and
it prints the move. A live-wt with local changes is not moved. ``.scio/recovery-wt`` is never
touched: only the owner moves the supervisor's own code (ADR 0019).

While busy, ``--when-idle`` checks again every ``--poll`` seconds (60) until ``--max-wait``
(4 hours), then exits 3 naming what it waited on. Exit 0 when moved (or already there), 1 when
the move failed, 3 when it never became idle.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
POLL_S = 60.0
MAX_WAIT_S = 4 * 3600.0
BUSY = 3


def _session_state() -> ModuleType:
    """``session_state.py`` beside this file (runner, main checkout, recovery reader), loaded
    by path under the key ``session_state.sibling`` uses."""
    path = SCRIPTS / "session_state.py"
    key = f"_session_tools_session_state_{abs(hash(str(path)))}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod  # @dataclass looks its module up there (I-174)
        spec.loader.exec_module(mod)
    return sys.modules[key]


def busy_reasons(main: Path, runner: Any, ss: ModuleType) -> list[str]:
    """What keeps live-wt from moving now; empty when idle."""
    activity = ss.recovery_status(main, runner)
    if not activity.read:
        return [f"the recovery status could not be read ({activity.error}); not taken as idle"]
    reasons = [f"supervised run: {line}" for line in activity.running]
    reasons += [f"open incident: {line}" for line in activity.incidents]
    return reasons


def _git(runner: Any, wt: Path, *args: str) -> tuple[int, str]:
    proc = runner.run(["git", "-C", str(wt), *args], timeout=300)
    return int(proc.returncode), (proc.stdout if proc.returncode == 0 else proc.stderr).strip()


def move(live: Path, runner: Any) -> int:
    """Fetch, then detach live-wt at ``origin/main``. Never another checkout."""
    code, dirty = _git(runner, live, "status", "--porcelain")
    if code != 0:
        print(f"live-wt: cannot read {live}: {dirty}")
        return 1
    if dirty:
        print(f"live-wt has local changes; not moved:\n{dirty}")
        return 1
    code, out = _git(runner, live, "fetch", "--quiet", "origin")
    if code != 0:
        print(f"live-wt: fetch failed: {out}")
        return 1
    _, old = _git(runner, live, "rev-parse", "HEAD")
    code, new = _git(runner, live, "rev-parse", "origin/main")
    if code != 0:
        print(f"live-wt: no origin/main: {new}")
        return 1
    if old == new:
        print(f"live-wt is already at origin/main {new[:7]}")
        return 0
    code, out = _git(runner, live, "checkout", "--quiet", "--detach", "origin/main")
    if code != 0:
        print(f"live-wt: checkout failed: {out}")
        return 1
    print(f"live-wt moved: {old[:7]} -> {new[:7]} (origin/main)")
    return 0


def main(argv: list[str] | None = None, runner: Any = None) -> int:
    ss = _session_state()
    ss.console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--when-idle", action="store_true", help="wait until idle, then move")
    ap.add_argument("--max-wait", type=float, default=MAX_WAIT_S, help="seconds (default 4 h)")
    ap.add_argument("--poll", type=float, default=POLL_S, help="seconds between checks (60)")
    ap.add_argument("--root", type=Path, default=ROOT, help="any checkout of the repository")
    args = ap.parse_args(argv)
    runner = runner or ss.Runner()
    main_co = ss.main_checkout(args.root, runner)
    live = main_co / ss.LIVE_WT
    if not live.is_dir():
        print(f"no {ss.LIVE_WT.as_posix()} in {main_co}")
        return 1
    deadline = runner.now() + (args.max_wait if args.when_idle else 0.0)
    while True:
        reasons = busy_reasons(main_co, runner, ss)
        if not reasons:
            return move(live, runner)
        if runner.now() >= deadline:
            print("live-wt not moved; waited on:")
            print("\n".join(f"  {r}" for r in reasons))
            return BUSY
        print(
            f"busy ({len(reasons)}): {reasons[0]}; checking again in {args.poll:.0f} s", flush=True
        )
        runner.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
