#!/bin/sh
# Download openWakeWord "hey jarvis" ONNX models + sherpa-onnx ASR models
# into ./models (binaries are never committed to git).
#
# Usage:  ./scripts/download_models.sh [sensevoice|small|full]
#   sensevoice (default): SenseVoice int8, 中英双语 (~163MB, 推荐)
#   small               : sherpa-onnx-paraformer-zh-small (~78MB, 中文, 更快)
#   full                : sherpa-onnx-paraformer-zh-int8 (~228MB, 中文, 更准)
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
MODE="${1:-sensevoice}"
case "$MODE" in
    sensevoice)
        URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
        UNPACKED="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
        DIR="asr_sense_voice_zh_en_int8"
        ;;
    small)
        URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2"
        UNPACKED="sherpa-onnx-paraformer-zh-small-2024-03-09"
        DIR="asr_paraformer_zh_small"
        ;;
    full)
        URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2"
        UNPACKED="sherpa-onnx-paraformer-zh-int8-2025-10-07"
        DIR="asr_paraformer_zh_int8"
        ;;
    *)
        echo "usage: $0 [sensevoice|small|full]" >&2
        exit 2
        ;;
esac

FILE="model.int8.onnx"
if [ ! -f "$DEST/$DIR/$FILE" ]; then
    echo "downloading ASR model ($MODE)..."
    TMP="$(mktemp /tmp/ova_asr.XXXXXX.tar.bz2)"
    curl -fL --retry 3 -o "$TMP" "$URL"
    tar -xjf "$TMP" -C "$DEST"
    rm -f "$TMP"
    mv "$DEST/$UNPACKED" "$DEST/$DIR" 2>/dev/null || true
fi
echo "OK: wake models + ASR($MODE) ready in $DEST"

