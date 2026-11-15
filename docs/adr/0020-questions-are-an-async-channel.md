# ADR 0020: A feature's design question parks the feature, goes to its issue and the dashboard, and the answer reaches the next attempt

- **Status:** Accepted, 2026-11-15. The owner chose the direction and both channels in #141, part 5. The details below are the orchestrator's.
- **Date:** 2026-11-15
- **Issue:** #141 (part 5). Follows #137 (the answer reaches the retried attempt) and #140 (a skip no longer stops the gate).
- **Amends:** ADR 0011 decision 5 (a stopped feature is one blocking human decision). ADR 0017 decision 10 (the dashboard never answers a stop).

## Context
The owner (#141): "Make questions to you a first-class, asynchronous channel. #137 is a symptom. Forward-looking agent systems treat a design question as normal, not as a failure that halts the build. The question goes to an issue or the dashboard. Other features keep building in parallel while it waits. Your answer goes into the next attempt automatically." Asked where the question should reach them, the owner answered "Both": the issue and the dashboard.

What happens today:
- A coder that needs a design decision ends its attempt with `escalate_human`, after `design decision needed: ...` on T0 or the worker's `design_question` on T1/T2.
- The product build then stops for one blocking decision: `retry`, `skip` or stop (`_feature_decision`). Nothing else builds until the owner answers, and the answer travels only as `retry: <answer>` (#137).
- Measured on graphcalc M4: P29 asked its question at 01:38 UTC. The answer came at 03:30, and in those 1.9 hours nothing else built. A skip then stopped the build a second time at the gate (#140).

## Decisions
1. **A design question parks the feature; it does not stop the build.**
   - An attempt parks when it ends with a design question: the worker's `design_question`, or a `design decision needed:` ledger line, whatever the tier (`scio.product.build.stop_question` finds it).
   - The product build records a `FeatureRun` with the new status `parked`, carrying `question` and a `question_id`. It asks no interrupt and goes on with the milestone's other features, singly or in batches (ADR 0018).
   - An attempt that fails without a question keeps today's decision stop. This ADR is about questions, not failures.
2. **The question goes to the feature's issue.**
   - With `--gh`, the build comments on the feature's issue in the product repository. The comment's first line is the marker `<!-- scio:question feature=<id> attempt=<n> id=<question_id> -->`, followed by the question, the attempt's last ledger lines, and how to answer.
   - The issue gets the label `needs-answer`.
   - Offline, the question is written to the feature's issue file under `.scio/product/issues/`, which is the offline issue.
3. **An answer is a comment that starts with `answer:`.** SCIO comments with the owner's own credentials, so the author cannot tell an answer from a question; the prefix can.
   - The first `answer:` comment after the question's marker answers it.
   - Offline, or from the dashboard when the product has no repository, an answer is a file `.scio/answers/<question_id>.md` in the workspace, outside the product's repository. A question's id is `<feature>-a<attempt>`, for example `P29-a1`, and stays the same across reruns.
   - `scio/product/questions.py` holds the contract: the marker, the prefix, the label, the answer directory, and `format_question`, `parse_answer`, `read_answer_file` and `write_answer_file`. It has pure functions only; callers do the `gh` calls. The build, the dashboard and the tests use it, and nothing else parses these formats.
4. **The build picks answers up itself.** At the start of each feature pass and before the milestone gate, the build reads the answers to its parked features.
   - A parked feature with an answer gets its next attempt, through #137's path: the answer and its question go into the task. Its `needs-answer` label comes off, and the build comments that the answer was received.
   - A parked feature without an answer stays parked.
5. **Only when nothing else can build does the milestone wait, once, for all its questions.**
   - When every other feature of the milestone is done, and parked features still have no answer, the build stops once at the phase `questions_wait`. The stop lists every open question with its issue link.
   - The reply `proceed` defers the parked features, as a `skip` does (#140: deferred, not undelivered). `stop` stops the build. Any other reply means "I answered on the issues": the build reads the answers again and goes on.
   - A rerun of `scio build`, by the operator or the recovery tick, does the same.
6. **The dashboard shows the open questions and can answer them.**
   - It lists every parked feature with its question, attempt, issue link and age.
   - Its answer box posts the `answer:` comment on the issue through `gh`, or writes the answer file when the product has no repository.
   - The dashboard still never writes a checkpoint. The build stays the only writer of its own state, as ADR 0017 requires. An answer is input the build reads on its next pass.
   - The endpoint binds to localhost, as the dashboard does, and takes a per-launch token that the page carries.
7. **Visibility:**
   - the `needs-answer` label in the product repository;
   - the dashboard's open-questions list;
   - in `scripts/metrics.py` (#141 part 2), time-to-answer as part of the stall time, and the number of features parked.

## Consequences
- While a question waits, the rest of the milestone keeps building. The only blocking stop left for questions is the single `questions_wait`, reached only when nothing else can build.
- The answer can come from anywhere, phone included. The build reads it on its own next pass.
- **New state:** `FeatureRun.status` gains `parked`, and `FeatureRun.question_id` is added (optional, so old checkpoints load). The phase `questions_wait` is new, and so is the reply `proceed` at it.
- **Unchanged:**
  - A failure without a question stops as it does today.
  - `skip` still defers (I-116, #140).
  - `retry: <answer>` at a feature stop still works (#137).
- **Open:** the recovery tick could watch `needs-answer` issues and rerun the build when an answer lands, so no one has to type the rerun. That needs a change to the supervisor (ADR 0019) and is left for later.
