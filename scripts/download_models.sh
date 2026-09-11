#!/bin/sh
# Download openWakeWord "hey jarvis" ONNX models + Silero VAD +
# sherpa-onnx ASR models
# into ./models (binaries are never committed to git).
#
# Usage:  ./scripts/download_models.sh [sensevoice|small|full]
#   sensevoice (default): SenseVoice int8, 中英双语 (~163MB, 推荐)
#   small               : sherpa-onnx-paraformer-zh-small (~78MB, 中文, 更快)
#   full                : sherpa-onnx-paraformer-zh-int8 (~228MB, 中文, 更准)
set -eu

DEST="$(dirname "$0")/../models"
GITHUB_PROXY="${GITHUB_PROXY:-}"
mkdir -p "$DEST"

download_file() {
    out="$1"
    url="$2"
    if [ -n "$GITHUB_PROXY" ]; then
        case "$url" in
            https://github.com/*)
                proxy="$GITHUB_PROXY/$url"
                echo "downloading via $proxy"
                curl -fL -C - --retry 3 -o "$out" "$proxy" && return 0
                echo "WARN: proxy download failed, retrying official URL"
                ;;
        esac
    fi
    if curl -fL -C - --retry 3 -o "$out" "$url"; then
        return 0
    fi
    case "$url" in
        https://github.com/*)
            proxy="https://gh-proxy.com/$url"
            echo "WARN: official download failed, retrying via $proxy"
            curl -fL -C - --retry 3 -o "$out" "$proxy"
            ;;
        *)
            return 1
            ;;
    esac
}

# --- wake word models (official openWakeWord v0.5.1) ---
WBASE="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
for f in hey_jarvis_v0.1.onnx embedding_model.onnx melspectrogram.onnx; do
    if [ ! -f "$DEST/$f" ]; then
        echo "downloading $f"
        download_file "$DEST/$f" "$WBASE/$f"
    fi
done
if [ -f "$DEST/SHA256SUMS" ]; then
    (cd "$DEST" && sha256sum -c SHA256SUMS) || echo "WARN: wake model checksum mismatch"
fi

# --- VAD model (sherpa-onnx-maintained Silero VAD, 16 kHz) ---
VAD_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
if [ ! -f "$DEST/silero_vad.onnx" ]; then
    echo "downloading Silero VAD model"
    download_file "$DEST/silero_vad.onnx" "$VAD_URL"
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
    TMP="$DEST/$DIR.tar.bz2"
    download_file "$TMP" "$URL"
    tar -xjf "$TMP" -C "$DEST"
    rm -f "$TMP"
    mv "$DEST/$UNPACKED" "$DEST/$DIR" 2>/dev/null || true
fi
echo "OK: wake models + Silero VAD + ASR($MODE) ready in $DEST"
