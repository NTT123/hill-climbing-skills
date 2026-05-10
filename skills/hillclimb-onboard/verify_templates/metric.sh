#!/usr/bin/env bash
# Metric verifier. Runs an evaluation and emits a single JSON line on stdout:
#   {"status":"pass"|"fail"|"inconclusive","score":<number>,"notes":"<string>"}
#
# Customize the EVAL_CMD below to point at your evaluation. It must print a
# single floating-point number on stdout (or set SCORE another way). Anything
# the eval prints is captured into "notes" for the dashboard.
#
# The "direction" of optimization (min vs max) lives in state.html, not
# here. This script just reports the score; hillclimb-verify decides
# whether it improved.

set -u
set -o pipefail

# --- EDIT ME ----------------------------------------------------------------
EVAL_CMD='python eval.py'   # must print a single number on its last stdout line
PASS_THRESHOLD=''           # optional: set e.g. PASS_THRESHOLD=0.9 to gate pass/fail.
                            # Empty means every numeric output is a "pass"; you
                            # rely on `best` tracking instead of a hard threshold.
DIRECTION='maximize'        # 'maximize' or 'minimize', only used if THRESHOLD is set.
# ---------------------------------------------------------------------------

raw_output=$(eval "$EVAL_CMD" 2>&1)
exit_code=$?
score=$(printf '%s\n' "$raw_output" | tail -n 1 | tr -d '[:space:]')

# Collapse the eval output into a one-line note, capped to keep the dashboard tidy.
notes=$(printf '%s' "$raw_output" | tr '\n' ' ' | cut -c1-400)

emit() {
  # $1=status, $2=score-or-empty, $3=notes
  python3 -c '
import json,sys
status,score,notes = sys.argv[1], sys.argv[2], sys.argv[3]
out = {"status": status, "notes": notes}
if score:
    try: out["score"] = float(score)
    except ValueError: pass
print(json.dumps(out))
' "$1" "$2" "$3"
}

if [ "$exit_code" -ne 0 ]; then
  emit fail "$score" "eval exited $exit_code: $notes"
  exit 0
fi

if ! [[ "$score" =~ ^-?[0-9]+\.?[0-9]*([eE][-+]?[0-9]+)?$ ]]; then
  emit inconclusive "" "could not parse a number from eval output: $notes"
  exit 0
fi

if [ -z "$PASS_THRESHOLD" ]; then
  emit pass "$score" "$notes"
  exit 0
fi

# Threshold path: one Python call decides pass/fail and emits the JSON.
# Pass the raw threshold string into the message so user-set "0.10" shows
# as "0.10", not "0.1" (float-then-format would strip trailing zeros).
python3 -c '
import json, sys
score = float(sys.argv[1]); threshold_str = sys.argv[2]
direction = sys.argv[3]; notes = sys.argv[4]
threshold = float(threshold_str)
ok = (score <= threshold) if direction == "minimize" else (score >= threshold)
miss = "above" if direction == "minimize" else "below"
out = {"status": "pass" if ok else "fail", "score": score,
       "notes": notes if ok else f"{miss} threshold ({direction} {threshold_str}): {notes}"}
print(json.dumps(out))
' "$score" "$PASS_THRESHOLD" "$DIRECTION" "$notes"
