#!/usr/bin/env bash
# Custom verifier. Implement whatever check you need; the only requirement
# is the output contract:
#
#   stdout: a single JSON object
#     {"status":"pass"|"fail"|"inconclusive","score"?:<number>,"notes"?:"<string>"}
#
# `score` is required if you want the chart and `best` tracking to update.
#
# Anything you write to stderr is ignored by hillclimb-verify (but visible in the
# terminal for debugging).

set -u
set -o pipefail

# --- IMPLEMENT ME -----------------------------------------------------------
# ... your checks here ...

# Example: emit a pass with a score
python3 -c 'import json; print(json.dumps({"status":"pass","score":0.0,"notes":"replace this with real verification"}))'
