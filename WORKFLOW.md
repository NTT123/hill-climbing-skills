# Hill-Climbing Workflow & Design

Why this system is shaped the way it is. `README.md` covers install/use; the
SKILL.md files are the trigger-time prompts. Read this when considering
changes or handing the project off.

---

## 1. What this is for

A class of problems has the same shape: some number matters (validation
loss, kernel throughput, page-load time, …); a single attempt is not
enough; each round is expensive enough you don't want to lose track of
what you tried; the endpoint is fuzzy (target hit, ideas dry, "good
enough").

These are *hill-climbing problems*: maintain a current best, try neighbors,
keep the best, escape local optima when stuck. The mechanism is the same
across ML hyperparameter sweeps, kernel optimization, and ablations;
only the verifier script and the candidate-generation strategy differ.

The hill-climbing skills cover this shape: open-ended iteration with an
explicit numeric objective, a re-runnable verifier script, an idea pool
that grows over time, and a separate brainstorm step that handles the
local-optimum case.

### 1.1 Honesty is the load-bearing assumption

The dashboard's value is entirely in the **trust** of its signal. A `best`
score that climbs because the executor gamed the verifier (edited
`verify.sh`, hardcoded the expected output, overfit to the eval set,
mocked-out checks) is *worse than no progress*, every downstream decision
is now made on a lie.

So the executor has one non-negotiable rule: **solve the real problem, not
the verifier**. A failed-but-honest run is a useful data point. A
passed-but-cheated run is pollution. `hillclimb-execute/SKILL.md`'s First
principle enumerates the anti-patterns; `hillclimb-brainstorm` mirrors the
prohibition so suggested ideas can't include verifier-loosening. See
[§ 9.13 No spec gaming](#913-no-spec-gaming).

---

## 2. The mental model

Classic hill climbing: maintain a current solution, ask "is there a better
neighbor?", move there if yes; if no neighbors are better, you're at a local
optimum, restart, diversify, or stop.

This system bends the analogy: "neighbors" aren't drawn from a defined
neighborhood function, they're an explicit *idea pool* proposed by the
user during onboarding and grown by an LLM during brainstorm. So the
workflow is closer to **guided local search with operator-generated
candidates** than canonical hill climbing. The name stuck because the
*picture* (current best, propose, evaluate, keep-or-reject, escape local
optima) is the right one for users.

| Hill-climbing concept | In this system |
|---|---|
| Current best solution | `state.best` |
| Candidate neighbors | `state.ideas[]` (status `open`) |
| Evaluate one neighbor | `hillclimb-execute` skill then `hillclimb-verify` skill |
| Compare to current best | `state.py verify-run` updates `best` if improved |
| Stuck (local optimum) | last N runs didn't improve `best` |
| Random restart / diversify | `hillclimb-brainstorm` skill injects new ideas |
| Stop | objective met, or user decides |

The user is the *strategy layer*: they choose the objective, write the
verifier, prune ideas, decide when to stop. Claude is the *mechanism
layer*: picks the next idea, executes, verifies, logs, surfaces the
picture.
This separation is deliberate: choosing the metric and candidate moves is
expert work; we don't try to automate it. We automate the bookkeeping.

---

## 3. Architecture at a glance

```text
The user invokes `hillclimb-onboard` once for interactive setup
(scaffolds `.hillclimb/` with `state.html`, `state.py`, `verify.sh`).

Then iterates three runtime skills:

  hillclimb-execute
    pick the top-priority idea, plan + do the work, log a run.
    Does NOT run the verifier.

       │
       ▼

  hillclimb-verify
    run `verify.sh`, parse the JSON contract on stdout, update `best`.

       │
       ├── open ideas remain?  ─── back to hillclimb-execute
       │
       └── stuck or no-signal?
              │
              ▼
           hillclimb-brainstorm
             diagnose (cold / promising / stuck / no-signal),
             add ideas, maybe reprioritize.
              │
              └── back to hillclimb-execute

Optional autopilot: `hillclimb-loop` runs the iteration above for
you, spawning subagents that invoke execute → verify → brainstorm
in turn, with per-iteration git checkpoints (rollback on fail or
inconclusive; code only, `state.html` keeps every failed-run record).
```

The user is the strategy layer; the four loop-role skills are the
mechanism. `hillclimb-loop` skill is composition on top, it doesn't replace
any of them, just runs them for you. Three things to notice:

1. **The user is in the loop.** Each step is user-invoked. There's no
   background daemon. `hillclimb-loop` skill automates the *cycle* via
   subagents and git checkpoints, but the user starts and stops it.
2. **Execute and verify are separate.** Verification is its own
   re-runnable step. Change the verifier, re-verify old runs without
   redoing the work.
3. **Brainstorm is explicit** in the manual flow. (`hillclimb-loop` skill
   auto-fires it on `STUCK_THRESHOLD`, but inside an explicit
   user-invoked loop.)

---

## 4. The skills

Each `SKILL.md` is the trigger-time prompt, `name` plus a `description`
that's the trigger surface for vague user requests. Below is the role of
each; the SKILL.md files are authoritative for the contract.

### 4.1 `hillclimb-onboard` skill

One-shot setup. Walks the user through ~10 questions one at a time, runs
a gap-detection subagent in parallel that scans the project files, and
scaffolds `.hillclimb/` (state files + helper CLI + time-limit wrapper +
verify template + .gitignore). Persists every answer immediately so
mid-flow abandonment leaves a coherent partial state. Pushes back on
vague ideas. Never executes.

### 4.2 `hillclimb-execute` skill

Picks the top-priority `open` idea, plans it, does the work, closes the
run with a summary + concrete actions. Owns the **First principle**
(anti-spec-gaming) and the in-progress invariant resolution (orphans get
finished or abandoned before any new work starts). Invokes the bundled
`simplify` skill before close-run (except for trivially small changes),
and applies the First principle to simplify's findings too. Wraps long
commands in `run_with_timeout.py` against `project.time_limit_seconds`.
May run `verify.sh` for spot-checks while working (seeing what your
changes produce is how you steer), but never persists a verification;
the canonical record and `best` update only come from `hillclimb-verify`
skill.

### 4.3 `hillclimb-verify` skill

Reads state, finds the latest unverified run, runs `verifier.command`
via the time-limit wrapper, and triages on the wrapper's exit code:

- exit `0` → defensive parse the last JSON-parseable stdout line for
  `{status, score, notes}`.
- exit `124` → `inconclusive` ("verifier timed out").
- other → `inconclusive` ("verifier exited N").

`state.py verify-run` then atomically attaches the verification, maps
the originating idea's status (pass → done, fail → abandoned,
inconclusive → open), updates `best` when status is `pass` and the score
improved per `direction`, and logs.

### 4.4 `hillclimb-brainstorm` skill

Diagnoses the search dynamic into one of four cases, **cold start /
promising / stuck / no-signal**, and biases idea generation accordingly.
For stuck and no-signal, also spawns a `general-purpose` diversification
subagent. Mirrors the First principle: never propose
verifier-loosening ideas. Three sharp ideas beat eight vague ones.

### 4.5 `hillclimb-loop` skill

Autonomous execute → verify → (when stuck) brainstorm. The orchestrator
never edits code or runs the verifier itself; it spawns a fresh
`general-purpose` subagent per phase using the canonical subagent-prompt
template (`/<skill>` + no-network-git + reply JSON contract), parses
replies, makes the keep-or-roll-back decision, and updates counters.

Per-iteration **git checkpoints** with rollback on failure: on
`fail`/`inconclusive`, `git checkout PRE_SHA -- . ':!.hillclimb/state.html'`
reverts tracked code but preserves the failed-run record in the dashboard
(see § 9.14). Stops on first match of: target met, max iterations,
stalled after brainstorm, out of ideas after brainstorm produced none,
`no-signal` brainstorm diagnosis.

### 4.6 `hillclimb` skill (end-to-end orchestrator)

Two phases, both delegated. Phase 1: read `state.html` and check
whether the project is "ready to loop" (objective described, verifier
command set, ≥ 3 ideas); if not, invoke the `hillclimb-onboard`
skill. Phase 2: invoke the `hillclimb-loop` skill, forwarding
positional args. The loop owns all pre-flight (git repo, dirty tree,
branch creation) and iteration 1 of every session via its Phase D
commit. There is no first-iteration special case in the orchestrator,
which keeps the file thin enough that no behavior can drift between
it and the loop.

---

## 5. The state file

The single source of truth is `<project>/.hillclimb/state.html`. It is
both the machine-readable state *and* the human-readable visualization,
intentionally fused into one file.

### 5.1 Structure

```text
state.html
├─ <html>, <head>, <style>          inline CSS, no external resources
├─ <body>
│   └─ #ledger                      the dashboard DOM (rendered by JS at view time)
├─ <script id="hillclimb-state"     ◀─── the JSON island
│            type="application/json">
│       { …state… }
│  </script>
└─ <script>                         the renderer, reads the island,
       (function () { … })()       builds the dashboard, draws the chart
   </script>
```

Skills only mutate the JSON inside the island. The renderer regenerates
the dashboard at view time, so the visualization can never drift from the
data.

### 5.2 Why HTML-with-JSON-island

Three alternatives we rejected:

- **Separate `state.json` + auto-rendered `dashboard.html`**, clean
  separation but two files to keep in sync, and the user has to know
  which to open.
- **HTML where skills regenerate the rendered body on each mutation**
  body-out-of-sync-with-JSON risk; skills doing HTML rendering through
  string editing is fragile.
- **Markdown for state plus a separate dashboard**, easy to grep but
  hard to keep schema-strict, and an extra file.

The JSON-island pattern keeps the dashboard always-current, makes
mutations trivially robust (replace one regex-matched block), and gives
the user one file to git-track or send to a colleague.

### 5.3 The schema

```json
{
  "created_at": "2026-05-09T08:05:41Z",
  "updated_at": "2026-05-09T08:05:41Z",
  "project": {
    "name": "Toy MSE Hunt",
    "objective": {
      "direction": "minimize | maximize",
      "target": 0.05,
      "unit": "MSE",
      "description": "minimize MSE on the holdout split"
    },
    "verifier": {
      "command": "bash .hillclimb/verify.sh"
    },
    "baseline": { "score": 0.42, "notes": "linear-regression baseline" },
    "stop_criteria": "score <= 0.05 OR 30 verified runs",
    "time_limit_seconds": 900
  },
  "ideas": [
    { "id": "I-001", "title": "polynomial features",
      "description": "add x^2, x^3 cross terms",
      "status": "open | in_progress | done | abandoned",
      "priority": "high | medium | low",
      "added_by": "onboard | brainstorm",
      "added_at": "2026-05-09T08:05:42Z" }
  ],
  "runs": [
    { "id": "R-001", "idea_id": "I-001",
      "started_at": "...", "ended_at": "...",
      "plan": "fit polynomial regression up to degree 3",
      "actions": ["edited model.py", "ran train.py"],
      "verification": { "status": "pass | fail | inconclusive",
                        "score": 0.30, "notes": "baseline poly3 done" },
      "summary": "fit poly3, MSE=0.30 on val",
      "commit": { "sha": "abc1234567...",
                  "message": "hillclimb-iter-3: pass score=0.30 (..., R-001)" } }
  ],
  "best": { "run_id": "R-002", "score": 0.18, "notes": "ridge alpha=0.5" },
  "log": [
    { "ts": "...",
      "kind": "onboard | execute | verify | brainstorm | note",
      "summary": "..." }
  ]
}
```

The verifier always emits the same JSON contract:
`{"status": "pass" | "fail" | "inconclusive", "score": <number>, "notes": "..."}`.
`score` drives `best` tracking and the chart. `objective.unit` is
optional; the chart Y-axis falls back to `SCORE`.

State invariants:

- `state.best` updates whenever a verified run with `status == "pass"`
  improves the score per `direction`. Empty until the first such run.
- `idea.status` for an idea referenced by an unverified run is
  `in_progress`; once verified, the status flows from the verification
  outcome (pass-done, fail-abandoned, inconclusive-open). The canonical
  signal is `runs[].verification`; `idea.status` is derived.
- At most one run has `ended_at == null` at any moment (the in-progress
  invariant, enforced by `state.py start-run`).

### 5.4 Mutations go through `state.py`

Non-negotiable. HTML editing through string replacement is brittle; the
`</script>` escape (any user-supplied note containing the literal
`</script>` would close the island) is centralized in one place; atomic
writes via `tempfile + os.replace` mean an interrupted skill never leaves
a half-written file; statuses, priorities, and the in-progress invariant
all validate in one module. `state.py` is the *only* place writes happen.
The SKILL.md files shell out to it; the dashboard JS only reads.

### 5.5 `state.py` subcommands

```text
init <path> --name <name>
read <path>
set <path> <dotted.key> <json-value>
append-idea <path> <idea-json> [--added-by onboard|brainstorm]
start-run <path> <idea_id> <plan>
finish-run <path> <run_id> [--summary STR] [--actions JSON-LIST]
verify-run <path> <run_id> <verification-json>
set-commit <path> <run_id> <sha> <message>
rollback-to <path> <run_id> [--force]
log <path> <kind> <summary>
```

Notable design choices:

- `set` walks dotted paths through dicts only. It **refuses** to index
  into lists and exits with a clear error. To mutate an item inside a
  list, read the full state, edit in your head, write the whole list
  back. (See § 9.8.)
- `start-run` enforces the in-progress invariant atomically. No two open
  runs.
- `verify-run` does *all* downstream work in one call (attach
  verification, map idea status, conditionally update `best`, append
  log). There's no separate `update-best`; the invariant should not be
  computable from outside.
- `set-commit` is populated by `hillclimb-loop` skill after each verified
  pass. Manual `hillclimb-execute` skill + `hillclimb-verify` skill flows
  leave the field unset; `rollback-to` refuses such runs with a clear
  error.
- `rollback-to` reads the run's stored `commit.sha` and runs
  `git checkout <sha> -- . ':!.hillclimb/state.html'`, restores the
  working tree without moving HEAD. Refuses dirty trees unless `--force`.
- All subcommands go through a `mutate(path)` context manager: read,
  yield for mutation, write, only on successful completion. Exceptions
  skip the write.

---

## 6. The verifier

`verifier.command` is a shell command. `hillclimb-verify` skill runs it
in `$PWD` via `run_with_timeout.py` and parses the last JSON-parseable
line of stdout. The contract:

```json
{ "status": "pass" | "fail" | "inconclusive",
  "score": 0.18,
  "notes": "ridge with alpha=0.5 cleanly beat poly3" }
```

`score` is required when `status` is `pass` (otherwise the chart and
`best` tracking can't update). `notes` is optional but recommended.

Two starter templates ship in
`skills/hillclimb-onboard/verify_templates/`:

- `metric.sh`: run an eval, parse one number, optional threshold gate.
  Default for ML / perf optimization.
- `custom.sh`: empty starter that documents the contract.

Anything else the verifier needs (running tests, hitting an endpoint,
calling a remote benchmark, asking a co-worker via Slack) lives inside
`verify.sh`. The plugin doesn't try to abstract those workflows; it
just expects the JSON contract on stdout.

Currently out of scope: native remote / async benchmarks (achievable
today via script polling but no first-class fire-and-wait), per-commit
regression baselines, dual-mode verification with disagreement
escalation. See section 11 Extending.

---

## 7. Workflow cadence over time

Three representative timelines.

### 7.1 Medium loop: an ML metric chase (2 days)

Onboard: minimize MSE, target 0.05, baseline 0.42 (linear regression),
5 initial ideas. Day 0: poly features → 0.30 (new best); ridge → 0.18
(new best); random forest → 0.25 (no improvement). Day 1: boosting →
0.22; deeper poly → 0.21. Three stuck runs trigger
`hillclimb-brainstorm` skill, diagnosis: stuck. Adds feature interactions
(high), early stopping (med), target encoding, bagging. Day 1: feature
interactions → 0.09 (new best). Day 2: early stopping → 0.07. The
chart shows `best` approaching the 0.05 target; user wraps up.

Without the brainstorm the search would have ground to a halt at 0.18.

### 7.2 Long loop: kernel optimization (2 weeks)

Onboarding takes 30 min, gap-detection subagent flags a missing
baseline benchmark; user runs it before continuing. 15 initial ideas
across 4 mechanism families. Week 1: 10 cycles, 3 improvements. First
brainstorm because the current best looked memory-bandwidth-limited; new
ideas in that family. Week 2: 8 more cycles, ceiling. Second brainstorm:
stuck. User adds an idea by hand: "rewrite in CUDA." That wins. User
ships at 92% of target.

The dashboard is the primary artifact across the entire two weeks. New
collaborators get up to speed by being sent the file.

### 7.3 Unsuccessful project, verifier was wrong (1 day)

A heuristic verifier produces suspiciously rising scores. User
spot-checks three outputs by hand and disagrees with all three; the
verifier measures something other than what they want. They rewrite
`verify.sh` to add a stricter check; subsequent verifies all return
`inconclusive`. Brainstorm correctly fires the **no-signal**
diagnosis, recommending verifier calibration before more iteration.
Runs scored under the old verifier remain on the chart but are no
longer comparable; the user clears `best` (`state.py set state.html
best '{"run_id":null,"score":null,"notes":""}'`) and abandons stale
ideas.

This is the only timeline that exercises the `no-signal` diagnosis.

---

## 8. Repository layout

```text
hill-climbing-skills/
├── .claude-plugin/
│   ├── plugin.json               ▸ plugin manifest (name: "hillclimb")
│   └── marketplace.json          ▸ marketplace catalog
├── skills/
│   ├── hillclimb/SKILL.md        ▸ end-to-end orchestrator
│   ├── hillclimb-onboard/
│   │   ├── SKILL.md              ▸ interactive setup prompt
│   │   ├── state.py              ▸ JSON-island read/write CLI
│   │   ├── run_with_timeout.py   ▸ per-command time wrapper
│   │   ├── template.html         ▸ self-rendering dashboard
│   │   └── verify_templates/{metric,custom}.sh
│   ├── hillclimb-execute/SKILL.md
│   ├── hillclimb-verify/SKILL.md
│   ├── hillclimb-brainstorm/SKILL.md
│   └── hillclimb-loop/SKILL.md   ▸ autopilot (subagents + git)
├── README.md                     ▸ install / use
└── WORKFLOW.md                   ▸ this file (design rationale; lives
                                     at the repo root, not in any skill)
```

After onboarding, a project under `<some-other-repo>/.hillclimb/`
contains: `state.html`, a pinned `state.py`, `run_with_timeout.py`,
`verify.sh`, and `.gitignore`. The skills operate against the
*project-local* helpers and have no dependency on the bundled-in-the-
plugin copies. This is what makes a project survive plugin uninstall.

---

## 9. Design decisions and rationale

### 9.1 Four loop-role skills + two optional orchestrators

**Considered.** A single mandatory orchestrator end-to-end. Also three
skills (collapse execute and verify) and five (add `hillclimb-prune` skill).

**Picked.** Four discrete loop-role skills, each user-invokable in
isolation. Plus two *optional* orchestrators that compose them:
`hillclimb-loop` skill (autopilot) and `hillclimb` skill (setup → loop
in one invocation, onboarding the only interactive step).

**Why.** Real iterative work has irregular cadence: three cycles in
ten minutes, then walk away for two days. A mandatory orchestrator
would either know when to stop (a strategy decision the user owns) or
run roughshod over the user's pace. Collapsing execute and verify
would make verification non-re-runnable; re-running the
`hillclimb-verify` skill against an old run is routine, and a separate
skill is what makes it cheap. Idea pruning, history queries, status
pages are expressible as `state.py` calls; every additional skill
costs trigger-surface complexity. Opt-in orchestrators give autopilot
users what they need while leaving the four primitives unchanged for
fine-grained control.

### 9.2 Project-scaffolded helper, not a shared global helper

**Considered.** Single shared `state.py` discovered at runtime.

**Picked.** `hillclimb-onboard` skill copies `state.py` into the project's
`.hillclimb/`. Each project pins its own copy.

**Why.** Hill-climbing projects have long lives. A shared helper means a
future bug fix or feature could change behavior of an in-progress
project mid-stream. With a pinned copy, the project's helper never
changes unless the user explicitly updates it; the user can uninstall
the plugin entirely and existing projects keep working.

**Cost.** A schema change in the plugin won't reach an in-progress
project until the user manually copies the new helper over. Section 11
documents the path; the trade-off is worth it for the long-running
projects this skill targets.

### 9.3 `state.html` is HTML-with-JSON-island, not split

See § 5.2 for the rationale. One caveat: the "dashboard cannot drift
from the data" claim holds *only if the JSON island parses*. A malformed
island produces a blank page rather than a visibly-wrong one.

### 9.4 Single verifier execution model (script)

**Picked.** A shell command that emits the JSON contract on stdout. No
dispatch on verifier "mode."

**Why.** Anything fancier (asking a human, calling an LLM judge,
hitting a remote benchmark) lives inside `verify.sh`. The plugin owns
the protocol (JSON on stdout, defensive parsing, exit-code triage) and
nothing else. Keeping a single execution path collapses what would
otherwise be three branches of code in `hillclimb-verify`, three
prompt scaffoldings, and three flavors of error handling into one.

### 9.5 Brainstorm is user-triggered, not auto-fired

**Considered.** Auto-fire `hillclimb-brainstorm` skill when the stuck
heuristic trips.

**Picked.** Explicit. The user decides when.

**Why.** Brainstorm costs tokens and attention. Auto-firing on every
detected stall would be noisy. The user knows when *they* think it's
time better than a 3-run heuristic. The stuck detection still happens,
but it runs *inside* the `hillclimb-brainstorm` skill as the
diagnosis, biasing what kind of ideas to generate.

### 9.6 Single dark theme, palette in CSS variables, no in-page toggle

**Picked.** A dark Observatory-inspired theme as the only theme. Inline
CSS, palette in CSS variables, no `prefers-color-scheme` override, no
toggle UI.

**Why.** The dashboard's value comes from the chart and the long-form
record, both of which read better in a single curated theme than under
the OS preference's guess. A toggle adds UI surface area for an
infrequent action. Variables stay in CSS so a future re-skin is a
single-file change.

### 9.7 In-progress invariant enforced in `state.py`, not just SKILL.md

**Picked.** Enforce in `state.py start-run` (refuses while another run
has `ended_at == null`). The complementary case, finished but
unverified, is enforced at the prompt level by `hillclimb-execute`'s
Step 2.

**Why.** Prompt-level constraints get violated under interruption or
when Claude resumes after a long pause. A code-level invariant fails
loudly. Orphaned runs are the single biggest threat to the dashboard's
signal, every other field is recoverable. The unverified-but-finished
case isn't a data-integrity problem (the verification field is just
`null`), so the prompt is the right layer.

### 9.8 `state.py set` refuses list traversal

**Picked.** Refuse with a clear error.

**Why.** A naive walk that auto-creates empty dicts on missing keys
would interpret a list at `ideas` as "not a dict" and silently replace
it. Without this guard, `set ideas.0.priority "high"` would wipe the
entire ideas array and
replacing it with `{"0": {"priority": "high"}}`. A real data-loss bug.
The fix is loud failure; the verbosity is a feature, list mutations
are a stronger commitment and deserve explicit consent.

### 9.9 `</script>` escape in the JSON serializer

**Picked.** Replace `</` with `<\/` on serialize, undo on deserialize.

**Why.** `</script>` inside the JSON island would close the `<script>`
tag mid-island and corrupt the file. A user could trip this with an
innocent code-review comment. The escape is a one-liner, transparent on
read, and standard practice.

### 9.10 No batch subcommand in `state.py`

**Picked.** Each subcommand is a fresh process: read, mutate, write.

**Why.** A batch API would complicate the "persist immediately after
each answer" invariant, the whole reason mid-flow abandonment leaves a
coherent partial state on disk. Onboarding's ~10–15 separate calls are
dominated by user think-time anyway, so per-call Python startup is
invisible. The guarantee is valuable; the optimization is not.

### 9.11 Distributed as a Claude Code plugin

**Picked.** `.claude-plugin/{plugin,marketplace}.json` plus `skills/`
at the root. Users install via `/plugin marketplace add`.

**Why.** Plugins are the canonical distribution mechanism: managed
updates, clean uninstall, namespaced invocation, and bundled-asset
paths via `${CLAUDE_PLUGIN_ROOT}` that work after the plugin is copied
to the local cache.

### 9.12 Hardcoded `<cwd>/.hillclimb/` path, not configurable

**Picked.** Hardcoded.

**Why.** One canonical location per working directory means the
dashboard is unambiguously "this project's." A configurable path adds a
discovery step every SKILL.md would have to explain. For monorepos that
need parallel hill-climbs, the workaround is `cd <subproject>`, each
subdirectory keeps its own `.hillclimb/`. The cost is real; the
alternative is a config surface to maintain forever.

### 9.13 No spec gaming

**Picked.** Spell it out, prominently, in the executor's prompt
(`hillclimb-execute/SKILL.md` First principle), reinforce in its rules,
and mirror the prohibition in `hillclimb-brainstorm/SKILL.md`.

**Why.** Spec gaming is a documented failure mode of capable optimizers:
when the only feedback signal is the verifier's pass/fail, a clever
executor *will* find ways to satisfy the literal check while violating
the spirit. Goodhart's Law. Common shortcuts: editing `verify.sh` to be
lenient, hardcoding the eval-set answer, mocking-out verifier-touched
code, deliberately weak interpretations. None move the real objective;
all produce a nominal `best` improvement that misleads the user.

The cost of *not* writing this down is asymmetric: when implicit, edge
cases erode the rule gradually and the dashboard's signal silently rots.
When explicit and internalized, the same edge cases produce a
stop-and-ask response. ~50 lines of prompt buy the entire project's
credibility. This is the only § 9 entry that's about agent behavior
rather than mechanism, but it's the one that makes the mechanism worth
using.

### 9.14 `hillclimb-loop` skill orchestrates via subagents and git, not in-process

**Picked.** Spawn a fresh `general-purpose` subagent per phase
(execute, verify, brainstorm) that invokes the corresponding `hillclimb-*`
skill via the Skill tool. Use git for per-iteration checkpoints with
selective rollback on failed verifications.

**Why subagents.**

- **Clean context per iteration.** A long loop in one context
  accumulates files read, decisions reasoned about, dead-ends tried.
  Subagents start fresh, what `hillclimb-execute` skill already assumes.
- **Orchestrator stays small.** Only loop bookkeeping (counters,
  decisions, git invocations) plus three-line replies. Many iterations
  without bumping the token budget.
- **Reuse, not duplication.** Each subagent runs the *actual* `hillclimb-*`
  skill, not a paraphrase. The First principle, the strict-reviewer
  prompt, the no-game rule, all enforced naturally because we invoke
  the real skills.

**Why git rollback for code, but not for `state.html`.** A failed
iteration's *code* changes are noise; the failed-run *record* is
information the user wants. `state.html` is gitignored, so git
operations never touch it; the loop's `:!.hillclimb/state.html`
pathspec is belt-and-suspenders should the gitignore ever fail to
apply. The full mechanism (selective checkout, no `git add -A`
follow-up, why state.html is excluded) lives in
`hillclimb-loop/SKILL.md` Phase D. This is the only asymmetry of its
kind in the system.

**Run-to-commit linking.** The `hillclimb-loop` skill records each
pass commit's SHA on its run record (`run.commit = {sha, message}`)
via `state.py set-commit`. `state.py rollback-to <run_id>` restores
that run's code into the working tree without moving HEAD, useful for
re-running the verifier on a past run or for inspecting the code state
of an earlier high-water-mark.

**Two rollback sites, same pathspec, different scope.** Phase D
(loop) commits the rollback as a checkpoint marker; `state.py
rollback-to` restores into the working tree without committing
(`git restore .` undoes it). Both leave untracked files alone. Phase D's
safety is the orchestrator-level checkpoint chain; `rollback-to`'s
safety is the `--untracked-files=no` dirty check, which only guards
tracked work because `git checkout SHA -- .` cannot clobber untracked
files anyway.

**Cost.** Two subagent spawns per iteration (three on brainstorm rounds)
add latency and tokens. For the default 100 iterations that's ~225
subagent calls, substantial, but the alternative (everything in-process)
hits the context limit on real ML projects. The git protocol assumes a single
working copy; concurrent loops in worktrees are out of scope.

---

## 10. Failure modes and what catches them

| Failure mode | Caught by |
|---|---|
| User aborts onboarding mid-flow | `state.py` persists every answer immediately; partial `state.html` is coherent |
| `hillclimb-execute` skill interrupted mid-run | Next `hillclimb-execute` skill sees the orphan via `runs[].ended_at == null`; resolves it before claiming a new idea |
| User pastes a note containing `</script>` | `</` → `<\/` escape in `state.py` serializer |
| `set ideas.0.priority` (intends to mutate one idea) | `state.py set` refuses list traversal with an explicit error rather than wiping ideas |
| Verify script crashes / prints garbage | `hillclimb-verify` walks lines from end, tries `json.loads` until one succeeds; falls back to `inconclusive` with truncated raw output as notes |
| Verify script hangs | `run_with_timeout.py` kills it at `time_limit_seconds`; verify records `inconclusive` on exit 124 |
| OS-level interruption mid-write | `state.py` writes to `<path>.tmp` then `os.replace`, atomic on POSIX |
| State file deleted | `[ -f "$STATE_HTML" ]` pre-check tells the user to run `hillclimb-onboard` skill first |
| Plugin uninstalled mid-project | Project's `.hillclimb/state.py` and `state.html` keep working with the pinned helper |
| Malformed JSON island (regex match fails) | `state.py read` exits with `no JSON island found`; the dashboard renders blank rather than corrupt |
| Malformed JSON (parses but invalid) | `state.py read` raises; the renderer's inline JS try/catch defaults to `state = {}` and shows the empty-state UI |
| Renderer reads a field the JSON doesn't have | Renderer uses `state.x \|\| {}` defaults; missing fields show as `-`. |

**Not caught, known single-writer assumption.** The
read-mutate-`os.replace` is atomic per call but **does not lock**. Two
`hillclimb-execute` skill invocations against the same `.hillclimb/` from two
terminals can interleave such that one writer's mutation is lost. The
skills target single-human, single-session workflow; multi-writer
coordination is out of scope. If you need it, wrap each mutation in
`flock` against the state file.

The pattern across all of these: **fail loudly, never silently degrade**.
A project that knows it's broken can be fixed; a project that *thinks*
it's healthy when it isn't poisons future decisions.

---

## 11. Extending the system

- **Different skill prefix.** Rename `skills/hillclimb/` AND
  `skills/hillclimb-*/` directories AND the `name:` frontmatter in
  each `SKILL.md`. Update README, `.claude-plugin/plugin.json`, and
  `.claude-plugin/marketplace.json`.
- **Different state location.** Each `SKILL.md` defines `STATE_HTML`
  and `STATE_PY`. Change in each `SKILL.md`.
- **Different schema.** Modify `state.py` (the schema is implicit in
  the subcommands) and `template.html`. Each project pins its own
  copy of these files; existing projects don't pick up the change
  until the user copies the new helper over.
- **Different chart.** Replace the Canvas-drawing block in
  `template.html`. Skills don't read the chart.
- **Different stuck threshold.** Change "last 3" in
  `hillclimb-brainstorm/SKILL.md`. Single edit.
- **More verifier patterns.** Add a new starter to
  `verify_templates/`. The contract stays `{status, score, notes}`;
  whatever the script does to compute that is up to you.

---

## 12. Non-goals

- **Auto-stop.** The system never decides for the user that the project
  is done. `hillclimb-verify` skill may say "stop criterion appears met,"
  but stopping is always an explicit user action.
- **Authentication, sharing, sync.** The dashboard is a static file.
  Sharing means sending the file. Multi-user editing is out of scope.

### 12.1 Deferred (might happen later)

- **Resource budgeting.** No tracking of compute time, dollar cost, or
  API quota beyond the per-command time limit. A budget field in the
  schema would be a small addition.
- **Multi-project parallelism within one cwd.** Skills hardcode
  `<cwd>/.hillclimb/`. Parallel hill-climbs need separate working
  directories. A `--project` flag could surface multiple state
  directories per repo if monorepo use becomes common.
- **Native remote / async verifiers.** Today's `verify.sh` handles
  polling but there's no first-class fire-and-wait pattern.
- **Multi-judge verification.** The single-script contract makes it
  easy to combine signals inside the script (run two checks, agree or
  fail), but the dashboard sees one verdict per run.

---

## 13. Glossary

- **Run**: a single attempt at one idea. Has a plan, actions, summary,
  and (eventually) a verification.
- **Idea**: a candidate direction. Status flow:
  `open → in_progress → {done | abandoned}` on a successful or failed
  verification, but `in_progress → open` on an inconclusive one.
- **Best**: the run that achieved the most-improved verified score.
- **Baseline**: the score before any iteration began.
- **Target**: the desired score. Dashboard's progress bar fills from
  baseline to target.
- **Stuck**: a heuristic on the last 3 verified runs failing to improve
  `best`. Triggers `hillclimb-brainstorm`'s diversification bias.
- **JSON island**: the `<script id="hillclimb-state" type="application/json">...</script>`
  block inside `state.html` where the canonical state lives.
- **Verifier contract**: `{status, score, notes}`.
- **In-progress invariant**: at most one run has `ended_at == null` at
  any moment. Enforced by `state.py start-run`.
- **Brainstorm diagnoses**: `cold` (≤1 verified run), `promising`
  (recent improvement), `stuck` (3+ stuck runs), `no-signal` (verifier
  output uncorrelated with quality). Each biases idea generation
  differently; `no-signal` stops the loop.
- **General-purpose subagent**: a Claude sub-process spawned via the
  `Agent` tool with `subagent_type: "general-purpose"`. Brainstorm uses
  one for diversification; `hillclimb-loop` skill uses one per phase.
