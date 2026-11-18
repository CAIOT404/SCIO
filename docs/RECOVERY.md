# The external recovery supervisor

The supervisor keeps a `scio build` alive through failures in SCIO itself. It runs as the build's parent process, outside SCIO, and uses the standard library only. When the build fails because of a SCIO harness fault, it does the following:
1. Preserves the build's state.
2. Opens a `recovery` issue.
3. Starts a Claude Code session that repairs SCIO in a tested PR.
4. Once the owner has merged that PR, moves `.scio/live-wt` to the merge commit. It never moves it backwards: a live-wt whose HEAD already contains the merge (the owner moved it to a newer `main` with `scripts/move_live_wt.py`) stays where it is, the timeline says "live-wt at <head> already contains the fix <sha>; not moved", and the next steps run on that HEAD. Its `uv.lock` is compared with that HEAD, so a lock change made on `main` after the repair is not blamed on it (I-237).
5. Checks that the repaired SCIO can read the halted thread.
6. Replays the original command, so the build resumes from its checkpoint.

The design and the owner's decisions are in [ADR 0019](adr/0019-external-recovery-supervisor.md).

The supervisor never merges. It never answers a human stop (merge, gate, release, a feature decision). It never writes the workspace's databases, and it never touches the product.

## Set up once

1. **Pin the supervisor's own code.** It runs from its own worktree, never from live-wt (which moves after every repair) or from the main checkout (which changes branches). Only the owner moves it:
   ```sh
   git -C <SCIO> worktree add --detach .scio/recovery-wt <sha on main>
   # later, to update the supervisor itself:
   git -C <SCIO>/.scio/recovery-wt checkout --detach <newer sha on main>
   ```
2. **Describe the workspace.** In the directory the SCIO launcher runs from (for graphcalc, `graphcalc-run/`), write `recovery.toml`:
   ```toml
   launcher = ["sh", "scio.sh"]          # the workspace's existing launcher, unchanged
   scio_repo = "C:/Users/<you>/OneDrive/Desktop/projects/SCIO"
   scio_gh_repo = "CAIOT404/SCIO"
   # live_wt = "<scio_repo>/.scio/live-wt"   (default)
   # claude_bin = "claude"                   (default)

   [limits]                               # optional; defaults in recovery/config.py
   hang_after_s = 2700                    # no progress for 45 minutes kills the build
   ```
3. **Register the scheduled tick.** The tick advances incidents and picks up builds orphaned by a reboot or by a dead supervisor. It runs at logon and every 10 minutes. Use PowerShell, not `schtasks` (I-045):
   ```powershell
   $wt = "C:/Users/<you>/OneDrive/Desktop/projects/SCIO/.scio/recovery-wt"
   $py = (Get-Command python -All | Where-Object Source -notlike "*WindowsApps*" | Select-Object -First 1).Source
   $action = New-ScheduledTaskAction -Execute $py -Argument "-m recovery tick" -WorkingDirectory $wt
   $triggers = @(
     (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
     (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10))
   )
   $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
     -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
   Register-ScheduledTask -TaskName "SCIO recovery tick" -Action $action -Trigger $triggers -Settings $settings
   ```
   `$py` skips the Microsoft Store alias under `WindowsApps`, which a PATH may list beside the real Python. Check that it names a real `python.exe`, or set it to that path yourself.

   The logon trigger names your user. Without `-User`, it fires at any user's logon, and Windows lets only an administrator register that: a normal shell gets "Access is denied." (I-200).

   Check the task once: `Start-ScheduledTask "SCIO recovery tick"`. When `Get-ScheduledTask "SCIO recovery tick"` reads `Ready` again, `Get-ScheduledTaskInfo "SCIO recovery tick"` shows `LastTaskResult` 0, and `state.sqlite` in the state folder (below) has a new write time. The repeating trigger's `Repetition.Duration` is empty, which means it repeats without end.

   The task's PATH has no `sh`. A replay therefore runs with the environment recorded from the original run, and every program is looked up on that environment's PATH (I-183).

   The 4-hour limit covers one tick running both repair attempts back to back, 2 × 90 minutes. A tick that is busy with a repair holds the tick lease, so the next scheduled ticks return at once (`IgnoreNew`).

## Run a build under supervision

Where you used `sh scio.sh build ...`, run the same arguments through the supervisor, from its worktree:

```sh
cd <SCIO>/.scio/recovery-wt
python -m recovery run --workspace <workspace> -- build --product <product dir> --gh <owner/repo>
```

`run` passes the build's output through and exits with the build's own exit code, so `PAUSED for a human:`, the resume hints and the exit status are exactly what `sh scio.sh` gives. When the build fails and the failure needs recovery, `run` prints one line naming the incident, and the recovery carries on in a background tick.

### Start a build no session owns

A build started with `run` is a child of the terminal or Claude session that started it, and it dies with that session. On 2026-11-15 a build was down for 200 minutes that way (I-246, issue #175). Start it detached instead, then wait for its stop:

```sh
cd <SCIO>/.scio/recovery-wt
python -m recovery run --detach --workspace <workspace> -- build --product <product dir> --gh <owner/repo>
python -m recovery wait --workspace <workspace>
```

`run --detach` starts the same supervised run as a detached process, prints `recovery run: detached run <run id> (pid <pid>); stdout: <path>` and returns 0 within seconds. The detached supervisor holds the lease, the stay-awake request and the hang watch, exactly as an attached `run` does. A busy workspace is refused the same way in both modes: the same line, exit 2, nothing started. If the process cannot start, or records no run within 10 seconds, `run --detach` says so and exits 1. What the detached supervisor itself prints goes to `runs/<run id>/supervisor.log`.

`wait` blocks until the workspace's newest run ends (`--run <run id>` names another), prints the last 60 lines of its `stdout.log` (`--tail N`) and exits with the run's exit code: 2 for a run that was refused, 1 for a run with no exit code (killed as hung, or an orphan whose supervisor and build are both gone). When the run opened an incident, it prints the same `recovery: incident ... opened` line as an attached `run`. When the run has already ended, it reports it at once, so `wait` can be killed and started again, by any session, and gives the same answer. It only reads the store: it takes no lease and writes nothing, so killing it never affects the build. A replay that a tick starts is the workspace's newest run, so the next `wait` follows it. Exit 2 with one line when there is no store, no run of that workspace, or no run with that id.

### Which thread a run records

`progress.json`'s `build_thread` is the thread that recorded the last finished milestone. `scio build` may run a different thread (I-163), so the run record treats that value as provisional. When the run ends, or a tick finds it orphaned, the supervisor records the thread SCIO actually ran (issue #123). It takes the first of these that names a thread:
1. the `thread` in SCIO's `outcome.json`;
2. the last `build thread <id> phase=...` line in the run's `stdout.log`;
3. the newest cost ledger in `<workspace>/.scio/ledger/` written since the run started, with a feature suffix such as `-p26-a4` removed. A build killed as hung has printed nothing yet, so this is the source that names its thread;
4. `progress.json` again.

The record's `build_thread_source` says which one it was (`outcome`, `stdout`, `ledger` or `progress`). The incident's evidence, the repair brief and the health check's `report --thread` all read the thread after the run ends. The dashboard reads it while the run is live, so until the run ends it shows the provisional `progress.json` thread.

## What happens to a failure

| The build ended with | The supervisor |
|---|---|
| done, or a human stop | records it; nothing else |
| a task fault (`integration_failed`, `gate_escalated`, `release_failed`, `stopped`, `budget`, `load_failed`) | records it as a closed incident; the owner decides, as before |
| a sign-in failure (`not logged in`, `invalid_grant`), or SCIO's pre-flight refusal (`scio: sign-in required: ...`, exit 2 before any work, issue #103) | opens an issue labelled `needs-human` and escalates in the same tick: sign in again (`claude auth login`), then re-run the build; no repair. A pre-flight that failed for another reason (`scio: pre-flight check failed: ...`) is read by its reason: a timeout is transient |
| a rate limit, a used-up usage window (`You've hit your 5-hour limit · resets 2:20pm (America/Los_Angeles)`), a GitHub refusal | replays after a backoff (up to 3 times), then escalates. When the message names a reset time, the backoff lasts until then plus 5 minutes, at most 8 hours after the failure, and the timeline says "waiting for the usage window to reset at <UTC time>". On a Python with no time zone database (Windows without `tzdata`, as the scheduled tick runs), the named time is read as this machine's local time, since `claude -p` runs here. A time it cannot read keeps the 5, 15 and 30 minute schedule (#126) |
| a traceback in SCIO's code, a harness error in a feature, or a hang | opens a `recovery` issue and starts a repair session; after the owner's merge, it updates live-wt, runs the health check and replays |
| a reboot or a dead supervisor (orphan) | replays; no repair |

**A hang is `hang_after_s` of awake time with no progress.** `recovery run` polls its build every `poll_s` seconds (15 by default) and kills it when no ledger, checkpoint or output file has changed for `hang_after_s`. Time the machine was down does not count (I-240, issue #158). When two polls are more than 5 minutes apart (or 4 poll waits, if that is longer), the machine was asleep or hibernating: the supervisor adds the gap to the run's `downtime_s`, notes "machine was down <n> h; not counted as idle" on the run, and leaves it out of the idle time. A build that makes no progress after the machine wakes is still killed after `hang_after_s` awake. `python -m recovery status` shows a run's `downtime` when it has some. While the build is alive, `recovery run` also asks Windows not to sleep on its idle timer (`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`, released when the build ends). Windows does not let that stop a sleep you ask for (closing the lid, the power button, Start > Sleep), which is why the downtime rule exists. A machine that lost power while asleep leaves an orphan, which the tick replays with no repair, as the table says.

The repair session runs `claude -p --permission-mode auto` in a fresh worktree under `.claude/worktrees/recovery-<incident>`. It works under SCIO's own CLAUDE.md and hooks: reproduce, ledger entry, sweep, regression test, guardrail, gate and a draft PR. It cannot merge, force-push, push to `main`, stash or call `gh api`. Its claims are checked against GitHub: the PR, CI on its head, a changed test and a ledger entry. The workspace's state must be unchanged.

## Commands

| Command | What it does |
|---|---|
| `python -m recovery run [--detach] --workspace W -- <SCIO arguments>` | a supervised build; with `--detach`, one that no session owns (above) |
| `python -m recovery wait --workspace W [--run RUN_ID] [--tail N]` | blocks until that workspace's run ends, prints the tail of its stdout and exits with its code; read-only, safe to kill |
| `python -m recovery status [--workspace W]` | runs (with their log directories) and open incidents |
| `python -m recovery watch <incident> [--follow] [--last N]` | one incident live: its state, its timeline, and the repair session's steps (each tool call and what the session says), read from Claude Code's transcript of the repair worktree; `--follow` prints only what is new until the incident closes or escalates |
| `python -m recovery incidents [--signature S] [--grep TEXT]` | searches incident history |
| `python -m recovery hold <incident>` and `release <incident>` | pause and resume an incident's automation |
| `python -m recovery dismiss <incident> --reason "<text>"` | ends an open incident that was not a SCIO fault (a held one too, such as a machine sleep read as a hang): state `dismissed`, with the reason on its timeline, in its status comment and in one comment on its issue. It starts no repair and no replay and never touches live-wt, and a GitHub failure is only a timeline note. A dismissed incident is not open: `status` lists it under "dismissed by the owner, not open", the tick ignores it, and `scripts/move_live_wt.py` does not wait on it. Exit 1, with nothing changed, for an incident that is already closed, escalated or dismissed, and while a tick is driving it (a running repair session: hold it, wait for the session to end, then dismiss) |
| `python -m recovery tick [--incident I]` | advance incidents now instead of waiting for the scheduled tick |

## Where things are

The supervisor's state lives under `%LOCALAPPDATA%/SCIO/recovery` (override with `SCIO_RECOVERY_HOME`). That keeps it outside OneDrive and outside every checkout.

| Path | What it holds |
|---|---|
| `state.sqlite` | runs, incidents, their timelines, the step log and leases |
| `runs/<run id>/` | each run's `stdout.log`, `stderr.log` and SCIO's `outcome.json`; for a detached run, also `supervisor.log` |
| `incidents/<incident id>/` | the snapshot taken before any repair: database backups, `progress.json`, git facts, logs |
| `incidents.jsonl` | one line per incident transition, for search |
| `replays/`, `ticks/` | logs of background replays and ticks |

Recorded incidents are what happened, not lessons. The investigated root cause is the ledger entry the repair PR adds (a fragment, `docs/ledger/I-NNN.md`). A validated lesson is the guardrail with a running guard (`docs/guardrails/NNN.md`).
