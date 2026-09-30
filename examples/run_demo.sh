#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python}"

if [[ ! -f "$HERE/demo_orders.csv" ]]; then
  "$PYTHON" "$HERE/make_demo_data.py"
fi

run() {
  echo
  echo "################################################################################"
  echo "QUESTION: $1"
  echo "################################################################################"
  "$PYTHON" "$HERE/jev_csv_query.py" "$HERE/demo_orders.csv" "$1"
}

run "Which country generated the most revenue?"
run "What was the average latency for failed requests?"
run "Which month had the highest average latency?"
run "How many failed requests came from Europe?"
run "Which product had the most successful orders?"
run "What was the average order amount for Enterprise orders?"
