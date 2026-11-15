Recovery supervisor fixes (issues #123, #126 and #130):
- It records the build thread SCIO actually ran: from the outcome file, then SCIO's output, then the ledger the run wrote, then `progress.json`. `build_thread_source` says which. Before, it took `progress.json`'s thread, the last finished milestone's.
- A used-up Claude usage window ("You've hit your 5-hour limit · resets …") is transient, never a repair, and the supervisor waits until the named reset time before it replays.
