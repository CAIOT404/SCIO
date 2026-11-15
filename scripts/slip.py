"""Count a slip in the machine's log, not the ledger (issue #141, part 3a).

    uv run python scripts/slip.py "<kind>" "<what happened>"

A slip is a mistake caught within the same turn with no effect: nothing committed, pushed,
published or left changed. It is counted, not ledgered, as the push-gate hook's refusals are
(W-18). A mistake that had an effect, or one that recurs, is a ledger entry
(``scripts/ledger_new.py``) as before.

It appends one JSON line ``{"at", "kind", "what", "cwd"}`` to ``slips.log`` in SCIO's
per-machine directory, where the gate keeps its lock (``scripts/gate.py``'s ``machine_dir``:
``%LOCALAPPDATA%/SCIO`` on Windows, ``~/.cache/scio`` elsewhere), never inside a repository.
``scripts/metrics.py`` counts them by kind. Keep the kind short and reuse it (``heredoc``,
``wrong-cwd``, ``wrong-worktree``), so the counts group.

Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
SLIP_LOG = "slips.log"
WHAT_CHARS = 300


def _gate() -> ModuleType:
    """``scripts/gate.py``, for its ``machine_dir``: one definition of the machine directory.
    Loaded by its path under the session tools' key (``session_state.sibling``)."""
    path = SCRIPTS / "gate.py"
    key = f"_session_tools_gate_{abs(hash(str(path)))}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod  # @dataclass looks its module up there (I-174)
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules[key]
        raise
    return mod


def slip_log_path() -> Path:
    path: Path = _gate().machine_dir() / SLIP_LOG
    return path


def log_slip(kind: str, what: str, cwd: str) -> Path:
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        "what": what[:WHAT_CHARS],
        "cwd": cwd,
    }
    path = slip_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0] if __doc__ else None,
        epilog="A slip had no effect; anything else is a ledger entry (scripts/ledger_new.py).",
    )
    ap.add_argument("kind", help="a short, reused name: heredoc, wrong-cwd, wrong-worktree")
    ap.add_argument("what", help="what happened, in one line")
    args = ap.parse_args(argv)
    kind, what = args.kind.strip(), " ".join(args.what.split())
    if not kind or not what:
        print("slip: give a kind and what happened", file=sys.stderr)
        return 2
    try:
        path = log_slip(kind, what, os.getcwd())
    except OSError as exc:
        print(f"slip: could not write the machine log: {exc}", file=sys.stderr)
        return 1
    print(f"slip: counted ({kind}) in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
