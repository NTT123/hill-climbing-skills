---
name: hillclimb
description: >
  End-to-end hill-climbing workflow: onboard the project if it isn't set
  up yet, then run the autonomous loop. Use when the user wants to set up
  and iterate in one invocation. Triggers: "run the hillclimb skill",
  "set up and iterate on X", "start a hill-climbing project end-to-end",
  "scaffold and run hill-climbing".
---

# hillclimb: onboard, then loop

Thin orchestrator. Two phases, both delegated; this file owns no logic
of its own.
## Phase 1: Load state, onboard if needed

```bash
STATE_PY="$PWD/.hillclimb/state.py"; STATE_HTML="$PWD/.hillclimb/state.html"
[ -f "$STATE_HTML" ] && STATE_JSON=$(python3 "$STATE_PY" read "$STATE_HTML")
```

The project is **ready to loop** when all of:

- `project.objective.description` is non-empty,
- `project.verifier.command` is non-empty,
- `ideas[]` has at least 3 entries.

If any of those is missing, or `$STATE_HTML` doesn't exist at all,
invoke `hillclimb-onboard` via the Skill tool. When it returns, re-run
the bash block above to refresh `STATE_JSON`, then re-check the three
conditions. If still not ready, the user bailed mid-onboarding;
surface a one-line summary of what's missing and stop.

## Phase 2: Loop

Invoke the loop with no arguments:

```text
Skill(skill="hillclimb-loop")
```

The loop owns its own pre-flight (git repo, dirty tree, branch
creation), reads `project.objective.target` and `project.stop_criteria`
from `state.py` to derive its own iteration cap and stop conditions,
runs until a stop condition fires, and produces its own final report.
This skill adds nothing after.

If the user expressed loop tuning intent in this invocation that is
**not** already captured in `state.py`, persist it there before
invoking the loop. Map phrasing to fields:

| User phrasing                                    | Field to set                                                                                                 |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| "iterate N times" / "loop N cycles" / "run N"    | `project.loop.max_iter` = `<int>`                                                                            |
| "be patient" / "don't brainstorm too eagerly"    | `project.loop.patient` = `true`                                                                              |
| "loop forever" / "until I interrupt" / "no stop" | `project.stop_criteria` = `"Loop runs until the user interrupts."` (semantic — the loop reads this freeform) |
| "iterate until target"                           | confirm `project.objective.target` is set; do not change `stop_criteria` if it already pins a count          |
| "lazy" / "keep every pass" / "don't roll back passes" | `project.loop.greedy` = `false`                                                                            |

Concrete persist call (one per field):

```bash
python3 "$STATE_PY" set "$STATE_HTML" project.loop.max_iter 5
python3 "$STATE_PY" set "$STATE_HTML" project.loop.patient true
```

Pass JSON literals (`5`, `true`), not strings (`"5"`, `"true"`); the
loop checks types, not truthiness. Don't translate intent into Skill
arguments; the loop has no argument path. The full field schema is
documented in `hillclimb-loop`'s "Read loop settings from state" section.

## Rules

- **Don't reimplement onboard or loop logic.** Phase 1 just decides
  whether onboarding is needed; Phase 2 just dispatches to the loop. The
  loop owns iteration 1 of every session via its Phase D commit, there
  is no "first iteration" special case here.
- **Don't add pre-flight here.** The loop has its own dirty-tree check
  and its own no-signal safeguard. Keeping this file thin is what
  guarantees the orchestrator and the loop can't drift apart.
- **Don't pass args to the loop.** All tuning lives in `state.py`. If
  the user expresses new intent, persist to state, then dispatch.
