---
name: hillclimb-execute
description: >
  Execute one iteration of a hill-climbing project: pick the next idea from
  the pool, plan it, do the work, log a run. Use this skill whenever the user
  has an active project with `.hillclimb/state.html` and wants to make
  progress. Trigger on phrases like "run the hillclimb-execute skill", "run the next iteration",
  "try the next idea", "do another pass", "execute one more", or simply
  "iterate" when a hill-climbing project is already set up. Does NOT run the
  verifier; that's the `hillclimb-verify` skill's job.
---

# hillclimb-execute: try one idea, log the run

## First principle: solve the real problem, not the verifier

The user wants the **actual objective** moved, not a number on a dashboard.
A run that "passes" by gaming the verifier is worse than a run that honestly
fails, false-pass results poison every later decision (which idea to try
next, when to stop, whether to brainstorm), and the user makes downstream
choices based on a lie. **A cheated solution is useless to the user.**

Concretely, do NOT, even when tempting:

- **Edit `verify.sh` to be more lenient**, change its threshold, comment
  out checks, or alter what it measures. The verifier is the user's
  contract; if it's wrong, that's a separate problem the user fixes
  deliberately.
- **Hardcode the expected answer**, overfit to the eval set, or special-case
  the verifier's inputs.
- **Stub or mock** functionality the verifier touches just to make it pass.
- **Make the verifier silently skip** what it was supposed to check
  early-returning, swallowing exceptions, exit-0 on internal error.
- **Pick a deliberately easy interpretation** of the idea when the user
  clearly meant something harder.

If you find yourself reasoning "this would technically satisfy the check but
isn't what the user actually wants," stop and either (a) honestly fail this
run with a clear summary, or (b) ask the user before proceeding. If the
verifier itself genuinely looks broken, **stop and tell the user**; add a
high-priority idea to fix it. Do not work around it. The dashboard's value
comes entirely from its signal being trustworthy.

## Step 1: Locate state and read it once

```bash
STATE_PY="$PWD/.hillclimb/state.py"; STATE_HTML="$PWD/.hillclimb/state.html"
[ -f "$STATE_PY" ] && [ -f "$STATE_HTML" ] || { echo "no .hillclimb/, run the hillclimb-onboard skill first"; exit 1; }
STATE_JSON=$(python3 "$STATE_PY" read "$STATE_HTML")
```

Reuse `STATE_JSON` for the in-progress check, idea selection, and the
time-limit lookup in Step 5, don't re-read.

## Step 2: Enforce the in-progress invariant

Inspect `STATE_JSON` for orphans before claiming a new idea:

- **Run with `ended_at: null`** (interrupted). If you can summarise what
  was attempted, finish it now: `python3 "$STATE_PY" finish-run "$STATE_HTML" "<R-id>" --summary "<what was done>"`. Otherwise ask the user (one
  `AskUserQuestion`) whether to finish (with their summary) or abandon
  (`idea.status = "abandoned"`).
- **Run with `ended_at` set but `verification == null`** (finished but
  unverified). Tell the user to run `hillclimb-verify` skill first, then stop.
  Don't start new work while previous work is unverified; that's how the
  hill loses its signal.

## Step 3: Pick the next idea

From `state.ideas`, take the first idea with `status == "open"`, ordered by
priority `high → medium → low`, then by id. Surface the choice in one
sentence (`"Picking I-007: feature engineering, adding lag features"`) and
proceed.

If no open ideas remain, stop and tell the user to run `hillclimb-brainstorm` skill.
Don't invent ideas in this skill.

## Step 4: Plan, then open the run

Write a one-paragraph plan: the concrete change, the files moving, the
expected effect on the metric, and what could go wrong. Then:

```bash
RUN_ID=$(python3 "$STATE_PY" start-run "$STATE_HTML" "<I-id>" "<plan paragraph>")
```

`start-run` atomically marks the idea `in_progress` and refuses if any
other run is still open.

## Step 5: Do the work, with a time budget

Execute the plan. Use whatever tools the task needs. **Stay focused on the
declared plan.** A different promising direction mid-run becomes a
follow-up idea, not a silent pivot.

For any bash command that could run for minutes (training, sweeps,
evaluation, kernel benchmarks), pull the project's per-command time limit
from the state JSON and wrap the command (the `or 600` is a defensive
fallback if the field is missing):

```bash
TIMEOUT=$(printf '%s' "$STATE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["project"].get("time_limit_seconds") or 600)')
# Invoke via the Bash tool in the foreground (run_in_background=false)
# background mode interferes with subagent dispatch. Pass the Bash tool's
# `timeout` ≈ TIMEOUT*1000 + 10000 ms so it doesn't kill the wrapper before
# cleanup. Bash caps that at 600000 ms (10 min); keep time_limit_seconds
# ≤ ~590 if you need the wrapper's exit-124 path to fire cleanly.
python3 .hillclimb/run_with_timeout.py "$TIMEOUT" bash -c "<your command>"
```

Exit `124` means the wrapper killed a hung command, finish the run
honestly with a summary noting the timeout. Don't silently retry with a
longer limit. Quick utility commands (`ls`, `grep`, package installs with
their own progress) don't need the wrapper.

If the work fails outright: either recover within the same run if the fix
is small (no scope creep), or bail, finish the run with a summary and let
`hillclimb-verify` catch the failure.

**Running `verify.sh` for spot-checks is fine**, seeing what your changes
produce is how you steer mid-run. What you must NOT do is persist a
verification from here (no `state.py verify-run` calls). The canonical
pass/fail/inconclusive record, and the `best` update, only come from
`hillclimb-verify` skill. Use `verify.sh` as an inspection tool here; the
recording happens in the next skill.

## Step 6: Simplify before closing

Invoke the `simplify` skill via the Skill tool. It reviews the changes
for reuse, quality, and efficiency and applies safe fixes. Running it
*before* `finish-run` means the simplified code is what the run's summary
describes and what `hillclimb-verify` skill evaluates next.

Skip simplify **only** for trivially small changes, a one-line config
tweak, a typo fix, a single parameter-value adjustment. Otherwise run it.
The First principle applies to its findings: **reject any proposal that
drops edge cases, weakens a check, or picks a stricter-to-looser
interpretation.** Note rejections in the run's `actions` so the next
iteration doesn't re-debate them. (`/simplify` spawns three review
subagents in parallel, real per-call cost worth knowing inside
`hillclimb-loop` skill.)

## Step 7: Close the run

```bash
python3 "$STATE_PY" finish-run "$STATE_HTML" "$RUN_ID" \
  --summary "<what was actually done; what to expect from verification>" \
  --actions '["edited src/model.py, replaced ReLU with GELU","ran train.py for 5 epochs"]'
```

Be concrete in the summary, name files, parameters, outcomes. Reflect
any `/simplify` changes (`"...; simplified: removed 2 dead helpers"`).
"Refactored a bit" is a bad summary.

## Step 8: Hand off

Tell the user: "run the `hillclimb-verify` skill to evaluate `<R-id>`." Don't run it yourself.

## Rules

- **Honest work only.** See First principle. A failed-but-honest run beats
  a passed-but-cheated one.
- **Always /simplify before close-run**, except for trivially small changes.
- One run per invocation. Never start a second idea in the same call.
- Never skip Step 2. Orphans corrupt the dashboard's signal.
- **Never persist a verification from here** (no `state.py verify-run`).
  Spot-checks via `verify.sh` are fine; they inform your work, but the
  canonical record and the `best` update only come from `hillclimb-verify` skill.
- Never edit `state.html` by hand. Always go through `state.py`.
- If the task is too big for one run, do the smallest verifiable slice and
  log the rest as a new idea via `append-idea`.
- If the verifier itself looks broken, stop and surface it as a
  high-priority idea. Don't work around it.
