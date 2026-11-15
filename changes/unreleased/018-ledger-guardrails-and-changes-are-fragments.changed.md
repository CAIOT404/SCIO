The ledger, the guardrails and the unreleased changes are fragments, one file each: `docs/ledger/I-NNN.md`, `docs/guardrails/NNN.md` and `changes/unreleased/`. So pull requests no longer conflict on them (issue #122, W-38 to W-43, I-206).
- `scripts/ledger_new.py` writes the next entry.
- `scripts/release_notes.py --cut` assembles the changes into the released section.
- `scripts/migrate_fragments.py` moves anything still written the old way.
- The docs check fails on a `TODO` left in a closed entry and on anything still written into the old files.
- The recovery repair session writes a ledger fragment.
