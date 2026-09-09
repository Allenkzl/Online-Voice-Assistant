#!/bin/sh
# Download openWakeWord "hey jarvis" ONNX models + sherpa-onnx Paraformer ASR
# models into ./models (binaries are never committed to git).
#
# Usage:  ./scripts/download_models.sh [small|full]
#   small (default): sherpa-onnx-paraformer-zh-small (~78MB, faster)
#   full           : sherpa-onnx-paraformer-zh-int8 (~228MB, more accurate)
set -eu

DEST="$(dirname "$0")/../models"
mkdir -p "$DEST"

# --- wake word models (official openWakeWord v0.5.1) ---
WBASE="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
for f in hey_jarvis_v0.1.onnx embedding_model.onnx melspectrogram.onnx; do
    if [ ! -f "$DEST/$f" ]; then
        echo "downloading $f"
        curl -fL --retry 3 -o "$DEST/$f" "$WBASE/$f"
    fi
done
if [ -f "$DEST/SHA256SUMS" ]; then
    (cd "$DEST" && sha256sum -c SHA256SUMS) || echo "WARN: wake model checksum mismatch"
fi

# --- ASR model ---
MODE="${1:-small}"
if [ "$MODE" = "full" ]; then
    URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2"
    DIR="asr_paraformer_zh_int8"
    FILE="model.int8.onnx"
else
    URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2"
    DIR="asr_paraformer_zh_small"
    FILE="model.int8.onnx"
fi
if [ ! -f "$DEST/$DIR/$FILE" ]; then
    echo "downloading ASR model ($MODE)..."
    TMP="$(mktemp /tmp/ova_asr.XXXXXX.tar.bz2)"
    curl -fL --retry 3 -o "$TMP" "$URL"
    tar -xjf "$TMP" -C "$DEST"
    rm -f "$TMP"
    mv "$DEST/sherpa-onnx-paraformer-zh-small-2024-03-09" "$DEST/asr_paraformer_zh_small" 2>/dev/null || true
    mv "$DEST/sherpa-onnx-paraformer-zh-int8-2025-10-07" "$DEST/asr_paraformer_zh_int8" 2>/dev/null || true
fi
echo "OK: wake models + ASR($MODE) ready in $DEST"
