---
name: hillclimb-verify
description: >
  Verify the most recent execution run in a hill-climbing project. Reads
  `.hillclimb/state.html`, runs the project's verifier script, and updates
  the dashboard with the result. Use this skill whenever the user finishes
  an `hillclimb-execute` and wants to know if it improved things. Trigger
  on "run the hillclimb-verify skill", "verify the last run", "evaluate", "check the
  result", or after any `hillclimb-execute` completes. Updates the
  dashboard's `best`, marks the originating idea done/abandoned/open, and
  reports back.
---

# hillclimb-verify: evaluate the latest run

See `hillclimb-execute` skill's First principle. False passes are worse than
false fails; when in doubt, mark `inconclusive` with the raw verifier
output as `notes`, never silently coerce malformed output into "pass."

## Step 1: Locate state, read it once

```bash
STATE_PY="$PWD/.hillclimb/state.py"; STATE_HTML="$PWD/.hillclimb/state.html"
[ -f "$STATE_PY" ] && [ -f "$STATE_HTML" ] || { echo "no .hillclimb/, run the hillclimb-onboard skill first"; exit 1; }
STATE_JSON=$(python3 "$STATE_PY" read "$STATE_HTML")
```

Use `STATE_JSON` for `verifier.command`, the run's plan/summary/actions,
the project objective, and the time limit.

## Step 2: Pick the run to verify

Walk `runs[]` from the end. Target the **first** run from the back where
`verification == null` AND `ended_at != null`.

- None found → "nothing to verify"; stop.
- Latest run has `ended_at == null` → tell the user the run isn't closed
  yet; suggest `hillclimb-execute` skill to finish it. Stop.
- User passed an explicit `<R-id>` argument → use that (re-verify case).

Capture `RUN_ID`, the run's plan + summary.

## Step 3: Run the verifier

Pull `verifier.command` and the per-command time limit out of `STATE_JSON`
(the `or 600` is a defensive fallback if the field is missing). If
`verifier.command` is empty, error with: "verifier.command is empty; set
it to a script command that emits the JSON contract." Invoke via the
Bash tool **in the foreground** (`run_in_background: false`), background
mode interferes with subagent dispatch. Pass the Bash tool's `timeout`
≈ `TIMEOUT*1000 + 10000` ms so it doesn't kill the wrapper before
cleanup; the Bash tool caps `timeout` at 600000 ms (10 min), so projects
with very long verifiers should keep `time_limit_seconds` ≤ ~590.

```bash
CMD=$(printf '%s' "$STATE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["project"]["verifier"]["command"])')
TIMEOUT=$(printf '%s' "$STATE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["project"].get("time_limit_seconds") or 600)')
python3 .hillclimb/run_with_timeout.py "$TIMEOUT" bash -c "$CMD" 2>&1
```

Wait for the completion notification. Capture stdout into `RAW`, the
wrapper's exit code into `EXIT_CODE`. Triage:

| `EXIT_CODE` | Verification to build |
|---|---|
| `0` | Walk `RAW`'s lines from the end and try `json.loads` until one succeeds; that's the verdict. If none parse: `{"status":"inconclusive","notes":"verifier returned no parseable JSON: <raw, truncated to ~400 chars>"}`. |
| `124` | `{"status":"inconclusive","notes":"verifier timed out after $TIMEOUT seconds"}`. Skip JSON parsing; output is unreliable mid-kill. |
| other | `{"status":"inconclusive","notes":"verifier exited $EXIT_CODE: <raw, truncated to ~400 chars>"}`. |

The exit-0 row's defensive parsing handles user scripts that emit prints
before the final JSON line.

## Step 4: Persist atomically

```bash
python3 "$STATE_PY" verify-run "$STATE_HTML" "$RUN_ID" '<verification-json>'
```

This single call: attaches the verification, maps the originating idea's
status (`pass→done`, `fail→abandoned`, `inconclusive→open`), updates `best`
when status is `pass` AND the score improved per `direction`, and appends
a log line. The `best` logic lives in `state.py`, don't reproduce it from
a separate `set` call.

## Step 5: Report back (one paragraph)

- `<R-id>: <status>` plus the score.
- Whether `best` was updated (and by how much vs. previous best).
- The originating idea's new status (`done` / `abandoned` / `open`).
- One nudge for the next step:
  - More open ideas → "run the `hillclimb-execute` skill for the next idea."
  - No open ideas → "run the `hillclimb-brainstorm` skill for more directions."
  - Stop criterion looks met → `Looks like the stop criterion is hit (<X>). Want to wrap up?`

## Rules

- Don't re-run the work, only the verifier. Artifacts of execution are
  already on disk.
- Never silently coerce malformed verifier output into "pass." When in
  doubt: `inconclusive` plus the raw output in `notes`.
- Never edit `state.html` by hand. Always go through `state.py`.
- The `best` update logic lives in `state.py verify-run`. Don't attempt
  to update `best` from a separate `set` call.
