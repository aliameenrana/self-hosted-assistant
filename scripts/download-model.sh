#!/usr/bin/env bash
set -euo pipefail

# Qwen3-8B Q4_K_M. Chosen for tool-call reliability, not chat quality.
REPO="Qwen/Qwen3-8B-GGUF"
FILE="Qwen3-8B-Q4_K_M.gguf"
DEST="$(cd "$(dirname "$0")/.." && pwd)/models"
URL="https://huggingface.co/${REPO}/resolve/main/${FILE}"

mkdir -p "$DEST"
if [ -f "$DEST/$FILE" ]; then
  echo "already present: $DEST/$FILE"
  exit 0
fi

EXPECTED=5027783488

echo "downloading $FILE (about 5GB) to $DEST"
# -C - resumes a partial .part file, so an interrupted run continues.
curl -L --fail --retry 5 --retry-delay 5 -C - -o "$DEST/$FILE.part" "$URL"

ACTUAL=$(stat -f%z "$DEST/$FILE.part" 2>/dev/null || stat -c%s "$DEST/$FILE.part")
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "incomplete: got $ACTUAL bytes, expected $EXPECTED. Re-run to resume."
  exit 1
fi

mv "$DEST/$FILE.part" "$DEST/$FILE"
ls -lh "$DEST/$FILE"
