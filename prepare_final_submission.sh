#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATASET_DIR="${1:-$HOME/Downloads/magicpin-ai-challenge/dataset/expanded}"

if [ ! -d "$DATASET_DIR" ]; then
  echo "Dataset not found: $DATASET_DIR"
  echo "Generate it first with:"
  echo "python ~/Downloads/magicpin-ai-challenge/dataset/generate_dataset.py --seed-dir ~/Downloads/magicpin-ai-challenge/dataset --out ~/Downloads/magicpin-ai-challenge/dataset/expanded"
  exit 1
fi

cd "$PROJECT_DIR"

echo "== Regenerating canonical submission.jsonl =="
python generate_submission.py "$DATASET_DIR"

echo "== Syntax check =="
python -m py_compile bot.py conversation_handlers.py generate_submission.py smoke_test.py

echo "== Submission count =="
python - <<'PY'
import json
from pathlib import Path
rows = [json.loads(x) for x in Path("submission.jsonl").read_text().splitlines() if x.strip()]
print(f"rows={len(rows)}")
if len(rows) != 30:
    raise SystemExit("Expected exactly 30 canonical rows. Stop before deploying.")
print("30 canonical rows ready.")
PY

echo "== Done =="
echo "Now run: python smoke_test.py"
