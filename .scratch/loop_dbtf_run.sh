#!/usr/bin/env bash
# Repeatedly runs `dbtf run --log-level debug` until it emits a
# "create or replace table" statement instead of "merge into" for an
# incremental model - i.e. until dbt rebuilds the incremental table from
# scratch instead of merging into it.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

iteration=0
while true; do
  iteration=$((iteration + 1))
  echo "=== [$(date '+%H:%M:%S')] iteration $iteration: dbtf run --log-level debug ==="

  # dbtf is a zsh alias (not a PATH binary), so it must be run through an
  # interactive zsh to resolve it - a plain subshell won't see it.
  zsh -ic 'dbtf run --log-level debug' >"$OUT" 2>&1
  status=$?

  if [ $status -ne 0 ]; then
    echo "iteration $iteration: dbtf run failed (exit $status). Last output:"
    tail -n 100 "$OUT"
    exit "$status"
  fi

  # source_rows is a plain table model (not incremental) - it legitimately
  # gets "create or replace table" every run, so exclude it here.
  CREATE_LINES="$(grep -ni "create or replace table" "$OUT" | grep -vi '`source_rows`')"

  if [ -n "$CREATE_LINES" ]; then
    echo "iteration $iteration: found 'create or replace table' on an incremental model - it was rebuilt from scratch. Stopping."
    echo "$CREATE_LINES"
    exit 0
  fi

  if grep -qi "merge into" "$OUT"; then
    echo "iteration $iteration: merge into (expected). Continuing."
  else
    echo "iteration $iteration: neither 'create or replace table' nor 'merge into' seen. Continuing."
  fi
done
