#!/usr/bin/env python3
"""
state.py: read/write the JSON island in `.hillclimb/state.html`.

The JSON island is a `<script id="hillclimb-state" type="application/json">…</script>`
block. All hill-climb skills mutate state through this CLI so writes stay robust
against HTML drift.

Subcommands (all paths are absolute or relative to cwd):
  init <path> --name <name>
      Copy the dashboard template into `path` and seed an empty state.
  read <path>
      Print the state JSON to stdout.
  set <path> <dotted.key> <json-value>
      Set a single field. Creates intermediate dicts as needed.
  append-idea <path> <idea-json> [--added-by onboard|brainstorm]
      Append an idea, auto-assigning id (I-001, I-002, …) and defaults.
  start-run <path> <idea_id> <plan>
      Open a new run, mark the idea in_progress. Prints the new run id.
  finish-run <path> <run_id> [--summary STR] [--actions JSON-LIST]
      Close out a run. Verification is set separately.
  verify-run <path> <run_id> <verification-json>
      Attach {status, score?, notes?} to a run; update best when status==pass
      and score improved; map idea status (pass→done, fail→abandoned,
      inconclusive→open).
  set-commit <path> <run_id> <sha> <message>
      Record {sha, message} on a run so its code can be restored later.
  rollback-to <path> <run_id> [--force]
      Restore that run's code into the working tree (HEAD does not move).
  log <path> <kind> <summary>
      Append an entry to the log timeline.

Exit codes: 0 on success, 1 on user error, 2 on unexpected I/O issues.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import subprocess
import sys

ISLAND_RE = re.compile(
    r'(<script id="hillclimb-state" type="application/json">\s*)(.*?)(\s*</script>)',
    re.DOTALL,
)

# Single source of truth for the schema's enum strings. The dashboard
# template duplicates these in CSS class names and JS; keep in sync.
IDEA_STATUS = ("open", "in_progress", "done", "abandoned")
PRIORITY = ("high", "medium", "low")
VERIFY_STATUS = ("pass", "fail", "inconclusive")
LOG_KIND = ("onboard", "execute", "verify", "brainstorm", "note")
ADDED_BY = ("onboard", "brainstorm")  # subset of LOG_KIND
VERIFY_TO_IDEA = {"pass": "done", "fail": "abandoned", "inconclusive": "open"}


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_run(state, run_id: str):
    run = next((r for r in state["runs"] if r["id"] == run_id), None)
    if run is None:
        sys.exit(f"run {run_id} not found")
    return run


def log_entry(state, ts: str, kind: str, summary: str) -> None:
    state["log"].append({"ts": ts, "kind": kind, "summary": summary})


def encode_for_html(obj) -> str:
    """JSON encode with `</` → `<\\/` escape so embedded notes can't break out
    of the <script> tag. Reversed transparently on read."""
    s = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)
    return s.replace("</", "<\\/")


def decode_from_html(island_text: str):
    return json.loads(island_text.replace("<\\/", "</"))


def read_state(path: str):
    if not os.path.exists(path):
        sys.stderr.write(f"state file not found: {path}\n")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = ISLAND_RE.search(text)
    if not m:
        sys.stderr.write(f"no JSON island found in {path}\n")
        sys.exit(1)
    return text, m, decode_from_html(m.group(2))


def write_state(path: str, text: str, m: re.Match, state) -> None:
    new_island = encode_for_html(state)
    new_text = text[: m.start(2)] + new_island + text[m.end(2) :]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, path)


@contextlib.contextmanager
def mutate(path: str):
    """Read-mutate-write context manager. Yields (state, ts) so every field
    written within a single mutation shares the same timestamp. Skips the
    write on exception so a failed mutation never half-persists."""
    text, m, state = read_state(path)
    ts = now()
    state["updated_at"] = ts
    yield state, ts
    write_state(path, text, m, state)


def empty_state(name: str) -> dict:
    ts = now()
    return {
        "created_at": ts,
        "updated_at": ts,
        "project": {
            "name": name,
            "objective": {
                "description": "",
                "direction": "minimize",
                "target": None,
            },
            "verifier": {
                "command": "bash .hillclimb/verify.sh",
            },
            "baseline": {"score": None, "notes": ""},
            "stop_criteria": "",
            "time_limit_seconds": 600,
        },
        "ideas": [],
        "runs": [],
        "best": {"run_id": None, "score": None, "notes": ""},
        "log": [
            {"ts": ts, "kind": "onboard", "summary": f'Project "{name}" initialized'}
        ],
    }


def cmd_init(args) -> None:
    template_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "template.html"
    )
    if not os.path.exists(template_path):
        sys.stderr.write(f"template not found: {template_path}\n")
        sys.exit(2)
    with open(template_path, "r", encoding="utf-8") as f:
        text = f.read()
    m = ISLAND_RE.search(text)
    if not m:
        sys.stderr.write("template missing JSON island marker\n")
        sys.exit(2)
    os.makedirs(os.path.dirname(os.path.abspath(args.path)), exist_ok=True)
    state = empty_state(args.name)
    new_text = text[: m.start(2)] + encode_for_html(state) + text[m.end(2) :]
    with open(args.path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"initialized {args.path}")


def cmd_read(args) -> None:
    _, _, state = read_state(args.path)
    print(json.dumps(state, indent=2, ensure_ascii=False))


def _walk_for_set(state, keys):
    """Walk a dotted-path through dicts only, creating intermediates. Refuse
    list traversal so a `set ideas.0.priority` exits loudly instead of silently
    wiping the ideas list."""
    obj = state
    for i, k in enumerate(keys[:-1]):
        path = ".".join(keys[: i + 1])
        if isinstance(obj, list):
            sys.exit(
                f"cannot traverse list with dotted path at '{path}'. `set` does not "
                f"index into lists, use a dedicated subcommand or read-mutate-write."
            )
        if k not in obj:
            obj[k] = {}
        elif isinstance(obj[k], list):
            sys.exit(
                f"refusing to overwrite list at '{path}', `set` cannot index list "
                f"elements. Use append-idea, start-run, verify-run, or log instead."
            )
        elif not isinstance(obj[k], dict):
            obj[k] = {}
        obj = obj[k]
    if isinstance(obj, list):
        sys.exit(f"cannot set leaf '{keys[-1]}' on a list at '{'.'.join(keys[:-1])}'.")
    return obj


def cmd_set(args) -> None:
    with mutate(args.path) as (state, _ts):
        keys = args.key.split(".")
        leaf = _walk_for_set(state, keys)
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
        leaf[keys[-1]] = value


def cmd_append_idea(args) -> None:
    idea = json.loads(args.idea_json)
    if "status" in idea and idea["status"] not in IDEA_STATUS:
        sys.exit(f"idea.status must be one of {list(IDEA_STATUS)}")
    if "priority" in idea and idea["priority"] not in PRIORITY:
        sys.exit(f"idea.priority must be one of {list(PRIORITY)}")
    with mutate(args.path) as (state, ts):
        idea.setdefault("id", f"I-{len(state['ideas']) + 1:03d}")
        idea.setdefault("title", "(untitled)")
        idea.setdefault("description", "")
        idea.setdefault("status", "open")
        idea.setdefault("priority", "medium")
        idea.setdefault("added_by", args.added_by)
        idea.setdefault("added_at", ts)
        state["ideas"].append(idea)
        log_entry(state, ts, args.added_by, f"Added idea {idea['id']}: {idea['title']}")
    print(idea["id"])


def cmd_start_run(args) -> None:
    with mutate(args.path) as (state, ts):
        # In-progress invariant: caller must resolve orphaned runs first.
        for run in state["runs"]:
            if run.get("ended_at") is None:
                sys.exit(
                    f"cannot start a new run while {run['id']} is still open. "
                    f"Call finish-run first."
                )
        run = {
            "id": f"R-{len(state['runs']) + 1:03d}",
            "idea_id": args.idea_id,
            "started_at": ts,
            "ended_at": None,
            "plan": args.plan,
            "actions": [],
            "verification": None,
            "summary": "",
        }
        state["runs"].append(run)
        idea = next((i for i in state["ideas"] if i["id"] == args.idea_id), None)
        if idea is None:
            sys.stderr.write(f"warning: idea {args.idea_id} not found in ideas[]\n")
        else:
            idea["status"] = "in_progress"
        log_entry(state, ts, "execute", f"Started {run['id']} for idea {args.idea_id}")
    print(run["id"])


def cmd_finish_run(args) -> None:
    with mutate(args.path) as (state, ts):
        target = find_run(state, args.run_id)
        target["ended_at"] = ts
        if args.summary is not None:
            target["summary"] = args.summary
        if args.actions is not None:
            target["actions"] = json.loads(args.actions)
        log_entry(state, ts, "execute", f"Finished {args.run_id}")


def _improved(score, current, direction):
    if current is None:
        return True
    if direction == "minimize":
        return score < current
    return score > current


def cmd_verify_run(args) -> None:
    verification = json.loads(args.verification_json)
    if verification.get("status") not in VERIFY_STATUS:
        sys.exit(f"verification.status must be one of {list(VERIFY_STATUS)}")
    with mutate(args.path) as (state, ts):
        target = find_run(state, args.run_id)
        target["verification"] = verification

        idea = next((i for i in state["ideas"] if i["id"] == target["idea_id"]), None)
        if idea is not None:
            idea["status"] = VERIFY_TO_IDEA[verification["status"]]

        obj = state["project"]["objective"]
        score = verification.get("score")
        if (
            verification["status"] == "pass"
            and score is not None
            and _improved(
                score, state["best"].get("score"), obj.get("direction", "maximize")
            )
        ):
            state["best"] = {
                "run_id": target["id"],
                "score": score,
                "notes": verification.get("notes", ""),
            }
        suffix = f" score={score}" if score is not None else ""
        log_entry(
            state, ts, "verify", f"{args.run_id}: {verification['status']}{suffix}"
        )


def cmd_set_commit(args) -> None:
    if not args.sha:
        sys.exit("sha is required")
    with mutate(args.path) as (state, ts):
        target = find_run(state, args.run_id)
        target["commit"] = {"sha": args.sha, "message": args.message}
        log_entry(state, ts, "execute", f"{args.run_id}: commit {args.sha[:7]}")


def cmd_rollback_to(args) -> None:
    _, _, state = read_state(args.path)
    target = find_run(state, args.run_id)
    commit = target.get("commit")
    if not commit or not commit.get("sha"):
        sys.exit(
            f"no commit recorded for {args.run_id}. "
            f"Was this run produced outside the hillclimb-loop skill?"
        )
    sha = commit["sha"]
    msg = (commit.get("message") or "").split("\n", 1)[0]

    work_dir = os.path.dirname(args.path) or "."
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=work_dir,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("not inside a git repository; rollback-to needs git")

    # `git checkout <sha> -- <pathspec>` silently overwrites locally-modified
    # tracked files, so this porcelain check is the only safety. Untracked
    # files aren't at risk (checkout never deletes untracked paths), hence
    # --untracked-files=no.
    if not args.force:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            encoding="utf-8",
        )
        if dirty.strip():
            sys.stderr.write(
                "refusing to rollback over modified tracked files:\n"
                f"{dirty}"
                "commit or stash these changes, or pass --force.\n"
            )
            sys.exit(1)

    subprocess.check_call(
        ["git", "checkout", sha, "--", ".", ":!.hillclimb/state.html"],
        cwd=root,
    )
    suffix = f": {msg}" if msg else ""
    print(f"rolled back to {sha[:7]}{suffix}")
    print("HEAD did not move, run `git restore .` to undo.")


def cmd_log(args) -> None:
    with mutate(args.path) as (state, ts):
        log_entry(state, ts, args.kind, args.summary)


def main() -> None:
    p = argparse.ArgumentParser(prog="state.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a new state.html from template")
    s.add_argument("path")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("read", help="print state JSON to stdout")
    s.add_argument("path")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("set", help="set a field by dotted path")
    s.add_argument("path")
    s.add_argument("key", help="dotted path, e.g. project.objective.target")
    s.add_argument("value", help="JSON value (bare strings also accepted)")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("append-idea")
    s.add_argument("path")
    s.add_argument("idea_json")
    s.add_argument(
        "--added-by", dest="added_by", default="onboard", choices=list(ADDED_BY)
    )
    s.set_defaults(func=cmd_append_idea)

    s = sub.add_parser("start-run")
    s.add_argument("path")
    s.add_argument("idea_id")
    s.add_argument("plan")
    s.set_defaults(func=cmd_start_run)

    s = sub.add_parser("finish-run")
    s.add_argument("path")
    s.add_argument("run_id")
    s.add_argument("--summary")
    s.add_argument("--actions", help="JSON list of action strings")
    s.set_defaults(func=cmd_finish_run)

    s = sub.add_parser("verify-run")
    s.add_argument("path")
    s.add_argument("run_id")
    s.add_argument("verification_json")
    s.set_defaults(func=cmd_verify_run)

    s = sub.add_parser("set-commit")
    s.add_argument("path")
    s.add_argument("run_id")
    s.add_argument("sha")
    s.add_argument("message")
    s.set_defaults(func=cmd_set_commit)

    s = sub.add_parser("rollback-to")
    s.add_argument("path")
    s.add_argument("run_id")
    s.add_argument(
        "--force", action="store_true", help="proceed even if the working tree is dirty"
    )
    s.set_defaults(func=cmd_rollback_to)

    s = sub.add_parser("log")
    s.add_argument("path")
    s.add_argument("kind", choices=list(LOG_KIND))
    s.add_argument("summary")
    s.set_defaults(func=cmd_log)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
