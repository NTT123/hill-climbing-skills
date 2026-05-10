---
name: hillclimb-loop
description: >
  Run the hill-climbing loop autonomously: spawn subagents that invoke
  the `hillclimb-execute` → `hillclimb-verify` → (when stuck)
  `hillclimb-brainstorm` skills, iteration after iteration, until a stop
  condition fires. Uses git for per-iteration checkpoints with automatic
  rollback on failed verifications. Trigger on phrases like
  "run the hillclimb-loop skill", "run the loop", "auto-iterate", "keep iterating",
  "run 10 cycles", "iterate until target", "loop forever", "no stopping",
  "until I interrupt", or any request for hands-off iteration on a
  project that's already onboarded. Requires `.hillclimb/`
  to exist (run the `hillclimb-onboard` skill first); on a dirty git
  tree, asks how to handle the pending changes (commit / stash / abort)
  before starting.
---

# hillclimb-loop: autonomous execute → verify → brainstorm loop

This skill orchestrates the cycle the user would otherwise run by hand. Each
phase runs in a **fresh `general-purpose` subagent** invoking the matching
hillclimb-* skill via the Skill tool. The orchestrator's main context stays
small and each iteration starts clean. **Git** provides per-iteration
checkpoints; failed iterations roll back code, successful ones become
commits on a `hillclimb-loop` branch the user can merge or discard. Each
pass commit's SHA is recorded on its run via `state.py set-commit`; later,
`state.py rollback-to <run_id>` restores that run's code.

The orchestrator never edits code or runs the verifier itself. It dispatches,
parses replies, decides keep-or-roll-back, and updates loop counters.

The First principle from `hillclimb-execute` applies recursively here, a
loop that commits cheated results across many iterations is catastrophic.
If a subagent's behavior makes you suspect spec gaming, stop the loop.

## Pre-flight

Bail on the first failure with a clear message.

```bash
STATE_PY="$PWD/.hillclimb/state.py"; STATE_HTML="$PWD/.hillclimb/state.html"
[ -f "$STATE_PY" ] && [ -f "$STATE_HTML" ] || { echo "no .hillclimb/, run the hillclimb-onboard skill first"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "the hillclimb-loop skill needs a git repo for checkpoints. Run \`git init\` first."; exit 1; }
```

If `git status --porcelain` is non-empty, ask the user (one
`AskUserQuestion`) whether to: (a) commit the pending changes on the
current branch, (b) `git stash push -u -m "pre-hillclimb-loop"` (the `-u`
captures untracked files too, onboarding's freshly-scaffolded
`.hillclimb/` artifacts are usually untracked), or (c) abort. Don't
silently include uncommitted work in iteration 1's diff. After stash,
re-check `git status --porcelain`, if it's still non-empty (rare:
ignored files survive stash), abort with the residual paths so the user
can resolve manually.

Then switch to (or create) the `hillclimb-loop` branch and capture the
pre-loop SHA so the final report can show only this loop's commits:

```bash
git rev-parse --verify hillclimb-loop >/dev/null 2>&1 \
  && git switch hillclimb-loop || git switch -c hillclimb-loop
ROOT_SHA=$(git rev-parse HEAD)
```

## Parse arguments

The argument string lives in `$ARGUMENTS` (the Skill tool's `args`
parameter expands here). Empty `$ARGUMENTS` means use defaults below.

| Setting           | Default | Meaning |
|-------------------|--------:|---------|
| `MAX_ITER`        |  `100`  | Hard cap on iterations |
| `STUCK_THRESHOLD` |    `3`  | Consecutive non-improvements before brainstorm fires |
| `STALL_BUDGET`    |    `5`  | Consecutive non-improvements after a brainstorm before stop |
| Stop on target    |  `true` | Stop early when `best.score` meets `objective.target` |

Examples (`$ARGUMENTS` value → effect):

| Args | Effect |
|---|---|
| (none) | all defaults |
| `5` | `MAX_ITER=5` |
| `until target` | `MAX_ITER=300`, stop-on-target stays on |
| `20 patient` | `MAX_ITER=20`, `STUCK_THRESHOLD=5` |
| `forever` | `MAX_ITER=100000`, `STALL_BUDGET=100000`. Stops only on target met, out-of-ideas after brainstorm, no-signal diagnosis, or user interrupt. Combines with `patient`. |

Refuse non-positive `MAX_ITER`/`STUCK_THRESHOLD` and unrecognized
tokens with a clear error listing the offenders. Valid tokens: positive
integers, `until target` (two-word phrase), `forever`, `patient`.
State a one-line plan to the user before starting (`"Running up to 100
iterations on hillclimb-loop branch; brainstorm after 3 stuck rounds, stop
on target."`) so they can interrupt early if it's wrong. For `forever`,
say so explicitly: `"Running until target / out-of-ideas / your Ctrl-C."`

## Subagent prompt template

Each phase below dispatches with this shape, fresh subagents inherit no
context, so each prompt is self-contained:

> *"Run the `/<skill>` skill via the Skill tool on the project at `$PWD`.
> Follow that skill's instructions exactly, including its First principle
> about honest work. Do NOT run `git push`, `git remote`, or any network
> git command.*
>
> *When done, reply with a single fenced `json` block containing only
> these keys (no extra prose, no logs, no file contents):*
> ```json
> <reply contract for this phase>
> ```
> *"*

## The loop

Initialize `ITER_N=0`, `NO_IMPROVE=0`, `BRAINSTORMS_DONE=0`. `NO_IMPROVE`
resets to `0` after every brainstorm, so the "stalled-after-brainstorm"
condition is just `NO_IMPROVE >= STALL_BUDGET AND BRAINSTORMS_DONE >= 1`
- no separate `POST_BRAINSTORM_RUNS` counter needed.

### Phase A: Checkpoint

```bash
ITER_N=$((ITER_N + 1))
PRE_SHA=$(git rev-parse HEAD)
```

### Phase B: Execute (subagent)

Dispatch the template with `<skill> = hillclimb-execute` and reply contract:

```json
{ "run_id": "<R-id or null>",
  "idea_id": "<I-id or null>",
  "status": "ok" | "blocked" | "error",
  "summary": "<one sentence>",
  "blocked_reason": "<set only if status is blocked>" }
```

Capture `RUN_ID`, `STATUS`. If `STATUS == "blocked"` and `blocked_reason`
indicates no open ideas:
- If `BRAINSTORMS_DONE == 0`: jump to Phase F without verifying; don't
  count this as an iteration.
- Else: stop with reason `"out of ideas after ${BRAINSTORMS_DONE} brainstorms"`.

### Phase C: Verify (subagent)

Dispatch the template with `<skill> = hillclimb-verify` and reply contract:

```json
{ "run_id": "<R-id>",
  "status": "pass" | "fail" | "inconclusive",
  "score": <number or null>,
  "notes": "<one sentence>" }
```

Capture `STATUS`, `SCORE`. Compute `IMPROVED` orchestrator-side:

```bash
IMPROVED=$(python3 "$STATE_PY" read "$STATE_HTML" \
  | python3 -c "import json,sys;s=json.load(sys.stdin);print('yes' if s.get('best',{}).get('run_id')=='${RUN_ID}' else 'no')")
```

### Phase D: Decide (orchestrator-side)

```bash
case "$STATUS" in
  pass)
    if [ "$IMPROVED" = "yes" ]; then
      MSG="hillclimb-iter-${ITER_N}: pass score=${SCORE} (new best, ${RUN_ID})"
      NO_IMPROVE=0
    else
      MSG="hillclimb-iter-${ITER_N}: pass score=${SCORE} (no improvement, ${RUN_ID})"
      NO_IMPROVE=$((NO_IMPROVE + 1))
    fi
    git add -A && git commit -m "$MSG" --allow-empty
    COMMIT_SHA=$(git rev-parse HEAD)
    python3 "$STATE_PY" set-commit "$STATE_HTML" "$RUN_ID" "$COMMIT_SHA" "$MSG"
    ;;
  fail|inconclusive)
    git checkout "$PRE_SHA" -- . ':!.hillclimb/state.html'
    git commit -m "hillclimb-iter-${ITER_N}: ${STATUS} (rolled back code, ${RUN_ID})" --allow-empty
    NO_IMPROVE=$((NO_IMPROVE + 1)) ;;
esac
```

**Why this exact rollback form** (load-bearing, do not "simplify" to
`git reset --hard`): the dashboard's value comes from showing every
attempt, including failures; chart, log, and brainstorm diagnosis all
depend on the failed-run record in `state.html`. `state.html` is
gitignored, so git operations don't touch it; the
`:!.hillclimb/state.html` pathspec is belt-and-suspenders should the
gitignore ever fail to apply (`git reset --hard` would still clobber it
in that case). The checkout form reverts every tracked path *except*
state.html in one operation, no temp files, no race window.

**No `git add -A` in the rollback branch.** The checkout already updated
the index for tracked paths, so the marker commit's tree equals
PRE_SHA's tree, an empty commit on top of HEAD. Adding `git add -A`
would promote the iteration's untracked artifacts (logs, checkpoints)
into tracked files baked into next iteration's PRE_SHA. Without it,
untracked files stay untracked in the working tree (the loop never
`git clean`s, those are usually what the user wants to inspect).

`--allow-empty` on the pass commit covers the case where the iteration's
only on-disk change was state.html (gitignored, so `git add -A` adds
nothing); on the rollback commit it covers the always-empty marker.

### Phase E: Stop conditions

Check in order; stop on the first match.

| # | Condition                                                      | Reason text |
|---|----------------------------------------------------------------|-------------|
| 1 | `objective.target` is set AND `best.score` crosses it          | `"target met"` |
| 2 | `ITER_N >= MAX_ITER`                                           | `"max iterations"` |
| 3 | `BRAINSTORMS_DONE >= 1` AND `NO_IMPROVE >= STALL_BUDGET`       | `"stalled after brainstorm"` |
| 4 | Out of ideas after a brainstorm produced none                  | from Phase F |
| 5 | `no-signal` brainstorm diagnosis                               | from Phase F |

Direction-aware target check: `<= target` for `minimize`, `>= target` for
`maximize`. If none triggers, continue to Phase F.

### Phase F: Brainstorm (conditional, subagent)

Trigger on `NO_IMPROVE >= STUCK_THRESHOLD` OR a Phase B "no open ideas"
short-circuit. Otherwise skip to Phase A.

Dispatch the template with `<skill> = hillclimb-brainstorm` and reply contract:

```json
{ "diagnosis": "cold-start" | "promising" | "stuck" | "no-signal",
  "ideas_added": <integer>,
  "titles": ["<title>", ...] }
```

Increment `BRAINSTORMS_DONE`; reset `NO_IMPROVE=0`.

- `diagnosis == "no-signal"` → stop with that reason; tell the user the
  verifier or objective needs fixing before further iteration is meaningful.
- `ideas_added == 0` AND no `open` ideas remain → stop with reason
  `"out of ideas; brainstorm produced none"`.
- Otherwise drop a marker commit and loop back. state.html is gitignored,
  so this commit has no diff content; it just chronologically marks where
  brainstorm fired in `git log`:

```bash
git commit -m "hillclimb-brainstorm-${BRAINSTORMS_DONE}: added ${IDEAS_ADDED} ideas" --allow-empty
```

## Final report

Show the user, in under 15 lines:

1. **Outcome.** `"Stopped after ${ITER_N} iterations: ${STOP_REASON}."`
2. **Improvements.** Baseline score, final `best.score`, delta absolute and
   percent. Chain of `best.run_id` updates over the run.
3. **Brainstorms.** Count, total ideas added.
4. **Git trajectory.** `git log --oneline ${ROOT_SHA}..HEAD` so the user
   sees iteration commits, improvements, no-improvements, rollbacks
   distinct.
5. **Next steps**, neutrally:
   - Stay on `hillclimb-loop` and run `hillclimb-loop` skill again.
   - `git switch <main> && git merge hillclimb-loop` (or cherry-pick).
   - `git switch <main> && git branch -D hillclimb-loop` to discard.
6. **Dashboard:** `file://${PWD}/.hillclimb/state.html`.

The dashboard is the long-form artifact; this hand-off is just orientation.

## Rules

- **Honest execution always.** Each subagent invokes the underlying skill,
  so its rules carry through. If you suspect a subagent gamed a check,
  stop the loop and surface it.
- **One iteration at a time.** Never spawn two execute subagents in
  parallel. The state's in-progress invariant catches concurrent writers,
  but the loop's logic assumes serialized iterations.
- **Subagent prompts must be self-contained.** Use the template above
  fresh subagents inherit no orchestrator context.
- **No network git commands.** The `hillclimb-loop` branch is local until
  the user explicitly merges or discards it.
- **No state.html rollback.** state.html is gitignored, and Phase D's
  pathspec exclusion is belt-and-suspenders against the gitignore ever
  failing to apply. Don't `git add -f .hillclimb/state.html`. The
  rationale lives in Phase D; don't paraphrase it elsewhere.
