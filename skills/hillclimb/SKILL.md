---
name: hillclimb
description: >
  End-to-end hill-climbing workflow: onboard the project if it isn't set
  up yet, then run the autonomous loop. Use when the user wants to set up
  and iterate in one invocation. Triggers: "run the hillclimb skill",
  "set up and iterate on X", "start a hill-climbing project end-to-end",
  "scaffold and run hill-climbing". Forwards positional arguments
  (iteration cap, `until target`, `forever`, `patient`) to `hillclimb-loop`
  via the Skill tool's `args` parameter. Verbatim when the user supplies
  them; otherwise translated from natural-language intent or onboarding
  answers.
---

# hillclimb: onboard, then loop

Thin orchestrator. Two phases, both delegated; this file owns no logic
of its own.

The user's args reach this skill via `$ARGUMENTS` (invoked as
`/hillclimb forever`, `/hillclimb 20 patient`, etc.). Phase 2 forwards
to `hillclimb-loop`, sometimes after translating natural-language
intent or onboarding answers into the loop's grammar.

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

Phase 2 also reads `project.stop_criteria` and `project.objective.target`
from `STATE_JSON`. Treat absent or empty values as "no signal."

## Phase 2: Loop

Decide the loop's args (rules below), then invoke the Skill tool with
`args` set explicitly:

```text
Skill(skill="hillclimb-loop", args="<args string>")
```

**Pass `args` whenever the user expressed any intent.** Without `args`,
the Skill tool drops the user's request silently and the loop runs with
defaults. For the genuine "use defaults" case, omit `args` or pass
`args=""`.

The loop owns its own pre-flight (git repo, dirty tree, branch
creation), runs until a stop condition fires, and produces its own
final report. This skill adds nothing after.

### Deciding the args (first match wins)

1. **`$ARGUMENTS` non-empty.** Pass `args="$ARGUMENTS"`. Don't re-parse.
2. **Natural-language intent in the invocation.** Map per the table
   below (full grammar in `hillclimb-loop` skill's "Parse arguments").
3. **Onboarding state.**
   - `stop_criteria` semantically means "loop until manual interrupt"
     (typical phrasings: "until I interrupt", "until user stops", "loop
     forever", "no automatic stop") → `args="forever"`. Use semantic
     judgment, not lexical matching: "user satisfied" or "manual
     review" do NOT mean forever.
   - `objective.target` is set AND `stop_criteria` doesn't pin an
     iteration count → `args="until target"` (300 iter cap vs the
     default 100; gives the target-met stop a real chance to fire).
4. **Nothing.** Omit `args`. Loop runs with defaults.

| User says                                              | `args=` value |
|--------------------------------------------------------|---------------|
| "set up and iterate" / "run end-to-end" (no count)     | (omit; loop defaults: 100 iter, brainstorm at 3 stuck) |
| "loop once" / "just one pass"                          | `"1"` |
| "loop 5 times" / "do 5 iterations"                     | `"5"` |
| "iterate until target"                                 | `"until target"` |
| "no stopping" / "loop forever" / "until I interrupt"   | `"forever"` |
| "be patient" / "don't brainstorm too eagerly"          | `"patient"` |

Args compose. `args="20 patient"` raises STUCK_THRESHOLD over 20
iterations; `args="forever patient"` is the unbounded version.

## Rules

- **Don't reimplement onboard or loop logic.** Phase 1 just decides
  whether onboarding is needed; Phase 2 just dispatches to the loop. The
  loop owns iteration 1 of every session via its Phase D commit, there
  is no "first iteration" special case here.
- **Don't add pre-flight here.** The loop has its own dirty-tree check
  and its own no-signal safeguard. Keeping this file thin is what
  guarantees the orchestrator and the loop can't drift apart.
- **Don't gate Phase 2.** Forward args; let the loop run.
