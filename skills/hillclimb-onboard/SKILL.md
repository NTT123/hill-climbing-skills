---
name: hillclimb-onboard
description: >
  Interactive onboarding for a hill-climbing optimization project. Use this
  skill whenever the user wants to start a long, iterative effort to optimize
  a measurable score, minimize a loss, maximize accuracy, or beat a perf
  target. Trigger on phrases like
  "set up a hill-climbing project", "run the hillclimb-onboard skill", "start an
  iterative project", "let's iterate on X with a dashboard", or when the
  user describes a problem whose solution requires many rounds of
  try-verify-revise. This skill walks the user through one question at a
  time, fills the gaps with a research subagent, and produces
  .hillclimb/state.html (a self-rendering dashboard) plus a verify.sh
  script template.
---

# hillclimb-onboard: set up a hill-climbing project

You are setting up a long-running iterative project. The output is a
self-contained `<cwd>/.hillclimb/` directory: `state.py` (helper CLI),
`state.html` (dashboard), `run_with_timeout.py` (per-command time wrapper),
`verify.sh`, `.gitignore`. After onboarding the other
skills call `<cwd>/.hillclimb/state.py` directly, no skill discovery, no
`_lib/`.

## Step 1: Locate bundled assets, handle existing state

```bash
SKILL_DIR="${CLAUDE_PLUGIN_ROOT}/skills/hillclimb-onboard"
SRC_STATE_PY="$SKILL_DIR/state.py"; TPL_DIR="$SKILL_DIR/verify_templates"
STATE_HTML="$PWD/.hillclimb/state.html"; STATE_PY="$PWD/.hillclimb/state.py"
```

If `$STATE_HTML` already exists, ask **continue** (patch missing fields
without re-asking the user about anything already set) or **start over**
(delete `.hillclimb/`).

Never silently overwrite. In particular: do **not** re-copy `verify.sh`
if it already exists in `.hillclimb/`, the user may have customised it.
If the user asks to change the template in continue mode, ask them
first.

## Step 2: Interview the user, ONE question at a time

Use `AskUserQuestion` for every question, one per call, never bundled.
After each answer, persist immediately via `state.py` (Step 4 has the
`init` block; run that right after Question 1 so `$STATE_PY` exists for
all subsequent persists). The user's attention drifts on long forms.

| #  | Field | Notes |
|----|-------|-------|
|  1 | Project name | short title for the dashboard |
|  2 | Objective description | 1-2 sentences on what success looks like |
|  3 | Direction | `minimize` or `maximize` |
|  4 | Target (optional) | numeric threshold the user wants to hit; the loop stops when `best.score` crosses it |
|  5 | Unit (optional) | short label (`MSE`, `TFLOPs/sec`, `accuracy`); free text via "Other"; falls back to `SCORE` on the chart |
|  6 | Verifier template | `metric` (compute one number, threshold-gate); `custom` (empty starter, you fill in the JSON contract). "Other" lets the user supply a verifier command directly without a template. |
|  7 | Baseline | the score on the unmodified tree. Suggest they run `bash .hillclimb/verify.sh` and paste the score; or `none` if greenfield. The chart's progress bar fills from baseline → target. |
|  8 | Stop criteria | e.g., "score ≤ 0.05", "all checks pass", "user satisfied". Free text for the dashboard; the loop's automatic target-met stop reads `objective.target`. |
|  9 | Initial ideas | ≥ 3 distinct directions; push back on 1-2 |
| 10 | Time limit per long-running command | minutes; presets `5 / 15 / 60` plus `Other` (free text). Stored as `seconds = round(minutes * 60)`. Caps every long bash invocation execute/verify spawn. On timeout, the wrapper kills the process tree and verify records `inconclusive` |

Persist examples (use after Step 4's `init`; same `set` shape for the rest):

```bash
python3 "$STATE_PY" set "$STATE_HTML" project.objective.direction '"maximize"'
python3 "$STATE_PY" set "$STATE_HTML" project.objective.target 1.0
python3 "$STATE_PY" set "$STATE_HTML" project.baseline '{"score": 0.42, "notes": "linear regression baseline"}'
python3 "$STATE_PY" set "$STATE_HTML" project.time_limit_seconds 900
python3 "$STATE_PY" append-idea "$STATE_HTML" '{"title":"feature engineering","description":"add lag features","priority":"high"}' --added-by onboard
```

## Step 3: Spawn a gap-detection subagent (in parallel with Step 2)

After you have the project name and objective description, launch a
`general-purpose` subagent that reads existing files in `$PWD`, compares
the user's objective against typical pitfalls (is the metric automatable?
is the baseline reproducible? are the initial ideas concrete?), and
returns ≤ 5 specific gaps with one suggested follow-up question each.
Cap response at ~200 words.

Then ask the user **one gap question at a time** via `AskUserQuestion`.
Persist each answer; stop when the user pushes back or all gaps addressed.

## Step 4: Scaffold `.hillclimb/`

After Question 1:

```bash
mkdir -p "$PWD/.hillclimb"
python3 "$SRC_STATE_PY" init "$STATE_HTML" --name "<project name>"
cp "$SRC_STATE_PY" "$STATE_PY" && chmod +x "$STATE_PY"
cp "$SKILL_DIR/run_with_timeout.py" "$PWD/.hillclimb/run_with_timeout.py"
chmod +x "$PWD/.hillclimb/run_with_timeout.py"
```

After Question 6 (verifier template), wire it up. Pick `metric.sh` or
`custom.sh` from `$TPL_DIR/` based on the user's answer.
**Refuse to overwrite an existing `verify.sh`**, if one is already there,
ask the user before replacing.

```bash
TEMPLATE="metric.sh"   # or custom.sh, per the user's answer
DEST="$PWD/.hillclimb/verify.sh"
if [ -e "$DEST" ]; then
  echo "verify.sh already exists, leaving it in place"
else
  cp "$TPL_DIR/$TEMPLATE" "$DEST" && chmod +x "$DEST"
fi
python3 "$STATE_PY" set "$STATE_HTML" project.verifier.command '"bash .hillclimb/verify.sh"'
```

If the user picked "Other" (no template, custom command), skip the `cp` and
set `project.verifier.command` to whatever shell command they supplied. The
command must emit a single JSON line on stdout: `{"status":"pass"|"fail"|"inconclusive","score":<num>,"notes":"..."}`.

From here on use `$STATE_PY` (the project-local copy). That's what the
other skills will use, so the smoke-test path matches.

## Step 5: Write `.hillclimb/.gitignore`

Ignore the dashboard plus scratch outputs. `state.html` is gitignored so
git checkout/reset can never destroy run history; the dashboard lives
only on disk.

```bash
cat > "$PWD/.hillclimb/.gitignore" <<'EOF'
state.html
*.tmp
*.log
__pycache__/
EOF
```

## Step 6: Confirm summary

```bash
python3 "$STATE_PY" read "$STATE_HTML"
```

Render a compact bullet summary: project name, objective (description,
direction, target, unit), baseline, stop criteria, verifier command, time
limit (`seconds / 60` minutes), ideas (id, title, priority).

Then one `AskUserQuestion`: **confirm** or **revise a specific field**.
On revise, update via `state.py set` (or `append-idea`) and loop. Don't
proceed until the user explicitly picks confirm.

## Step 7: Hand off (under five lines)

- Dashboard path: `file://<cwd>/.hillclimb/state.html`
- Path to `verify.sh` if applicable, with a one-line nudge to edit it
  before the first `hillclimb-verify`.
- Note that `.hillclimb/state.py` and `.hillclimb/run_with_timeout.py` are
  project-local helpers, don't move or delete.
- Next: `hillclimb-execute` skill to try the first idea.

## Rules

- **One question at a time.** Never bundle.
- **Persist immediately** after each answer. A user who abandons mid-flow
  should leave a coherent partial setup.
- **Push back on vague ideas.** "Try better hyperparameters" is not an
  idea; "Sweep learning rate over [1e-4, 1e-2] with cosine schedule" is.
- **Never edit `state.html` by hand.** Always go through `state.py`.
- **Don't run the verifier or execute ideas here.** Onboarding is setup
  only, `hillclimb-execute` and `hillclimb-verify` come next.
