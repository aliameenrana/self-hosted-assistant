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

echo "downloading $FILE (about 5GB) to $DEST"
curl -L --fail --progress-bar -o "$DEST/$FILE.part" "$URL"
mv "$DEST/$FILE.part" "$DEST/$FILE"
ls -lh "$DEST/$FILE"
