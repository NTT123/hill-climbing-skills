# Hill-Climbing Skills

Claude Code skills for problems that need many rounds of try → verify →
revise: optimizing a measurable score or chasing a perf target. State
lives in a `state.html` file you can open in any browser.

## Skills

| Skill                              | What it does                                                                |
|------------------------------------|-----------------------------------------------------------------------------|
| `hillclimb` skill             | End-to-end: onboard if needed, then hand off to the autonomous loop.        |
| `hillclimb-onboard` skill     | Interactive setup: objective, verifier, baseline, initial ideas.            |
| `hillclimb-execute` skill     | Pick the top-priority idea, plan + do the work, log a run.                  |
| `hillclimb-verify` skill      | Run the verifier, update `best`, mark idea done/abandoned/open.             |
| `hillclimb-brainstorm` skill  | Diagnose the search and add new ideas when stuck.                           |
| `hillclimb-loop` skill        | Autopilot: subagents run execute → verify → brainstorm, with git checkpoints. |

For a fresh project, run `hillclimb` skill. To drive the cadence by
hand, alternate `hillclimb-execute` skill and `hillclimb-verify` skill,
calling `hillclimb-brainstorm` skill when stuck.

## Install

This repo is a Claude Code plugin. Inside Claude Code:

```text
/plugin marketplace add NTT123/hill-climbing-skills
/plugin install hillclimb@hill-climbing-skills
```

To update: `/plugin update hillclimb@hill-climbing-skills`. To remove:
`/plugin uninstall hillclimb@hill-climbing-skills`.

## Example: optimize a kernel for TFLOP/s

You already have `kernel.py` (a working fp32 causal self-attention
kernel running at 3.6 TFLOP/s) and `check.py` (times the kernel and
checks correctness against torch SDPA). You want to beat SDPA
(~31 TFLOP/s) on RTX 5090. In Claude Code:

> Run the `hillclimb` skill: set up hill-climbing to maximize TFLOP/s
> on `kernel.py`. Baseline 3.6 TFLOP/s; target beats torch SDPA on
> B=1 H=32 S=4096 D=128. Verify with `check.py`; it already prints a
> TFLOP/s number.

Onboarding asks a few questions (objective, direction, target, baseline,
≥ 3 initial ideas like FlashAttention-style tiling, Triton port, TF32
mma.sync), drops a `verify.sh` template you point at `check.py`, and
hands off to the autopilot. The dashboard tracks a trajectory like:

```text
R-001  ✓ pass  14.12 TFLOP/s      FlashAttention-2 tiled
R-002  ✓ pass  16.72 TFLOP/s      TF32 mma.sync on QK^T
R-003  ✓ pass  24.35 TFLOP/s      Triton port, num_warps=4
R-004  ✓ pass  33.02 TFLOP/s ▲    BLOCK_M=32 sweep, beats SDPA
```

Open `.hillclimb/state.html` for the full picture: the score chart,
the idea pool, and each run's plan and summary.

## Project layout after onboarding

```text
<your-project>/
└── .hillclimb/
    ├── state.html           # the dashboard, open in any browser (gitignored)
    ├── state.py             # JSON-island read/write CLI
    ├── run_with_timeout.py  # per-command time wrapper (used by execute/verify)
    ├── verify.sh            # the verifier, emits {status,score,notes} JSON
    └── .gitignore
```

`state.html` is gitignored so `git checkout` / `git reset` can never
destroy run history; the dashboard lives only on disk.

## Verifier

`verify.sh` is a shell script that emits one JSON line on stdout:

```text
{"status": "pass" | "fail" | "inconclusive", "score"?: <number>, "notes"?: "..."}
```

`status` is required; `score` and `notes` are optional. `score` is
required when `status` is `pass` for the chart and `best` to update.
`status` drives idea-status mapping (pass → done, fail → abandoned,
inconclusive → open).

Onboarding ships two starter templates at
`skills/hillclimb-onboard/verify_templates/`: `metric.sh` (compute one
number, optional threshold-gate) and `custom.sh` (empty starter).

## Rollback to a past run

`hillclimb-loop` skill records each pass commit's SHA on its run record. To
restore a run's code into the working tree (HEAD does not move):

```bash
python3 .hillclimb/state.py rollback-to .hillclimb/state.html R-005
# … inspect or re-run verify.sh …
git restore .   # back to where you were
```

Manual `hillclimb-execute` skill + `hillclimb-verify` skill runs (outside the
loop) don't auto-record commits, so `rollback-to` will say `no commit
recorded for <R-id>`. To opt in: commit the working tree after each
verify and store the SHA via `state.py set-commit`.

## Develop locally

To hack on the skills without going through the marketplace, clone the
repo and add it as a local plugin source:

```text
/plugin marketplace add /path/to/hill-climbing-skills
/plugin install hillclimb@hill-climbing-skills
```

Edits propagate after `/plugin marketplace update hill-climbing-skills`
(or restart Claude Code).

## More

- **`WORKFLOW.md`**, design rationale, full schema, scenarios, every
  knob worth knowing.
- **`skills/<name>/SKILL.md`**, each skill's own contract.

Skills only mutate `state.html` through `.hillclimb/state.py`, never
edit it by hand.
