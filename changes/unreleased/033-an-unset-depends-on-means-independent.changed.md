A milestone feature the planner gave no `depends_on` no longer turns a parallel build serial (issue #157, ADR 0018 amended).
- The milestone planner prompt (v3) requires `depends_on` for every feature: `[]` when the feature's code and tests would not need an earlier feature's code to exist first, else the ids it needs.
- With `SCIO_PARALLEL_FEATURES` above 1, `normalise_milestone` turns an unset `depends_on` of a newly planned milestone into `[]`, and one ledger note names the features.
- A plan reused from `Mn-plan.json` is not rewritten: its unset `depends_on` still means the feature before it. With `SCIO_PARALLEL_FEATURES=1` nothing changes.
