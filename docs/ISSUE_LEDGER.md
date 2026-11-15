# Issue ledger

Running log of every problem hit while building SCIO, with how and when it was resolved. Add an entry when the problem is found; complete it when it is resolved. Each resolved entry records the **sweep**: the search made for the same mistake elsewhere and what it found. Statuses: `open`, `resolved`, `wontfix` (with reason).

**The entries are files**, one per entry, named by its id: `docs/ledger/I-NNN.md` (issue #122, W-38). Two pull requests that each add an entry no longer conflict here. [ledger/README.md](ledger/README.md) has the template and how to write an entry; `uv run python scripts/ledger_new.py --title "<short title>" --where "<file, command or step>"` writes the next free id's file. The docs check reads the entries there, and fails on an entry written into this file. `cat docs/ledger/I-*.md` prints the whole ledger in id order.
