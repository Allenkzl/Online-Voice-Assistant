# Model notices for Online Voice Assistant

The repository **never contains model binaries**. They are downloaded at
setup time by `scripts/download_models.sh` and verified with checksums.

## Wake word model (openWakeWord)

- Files: `hey_jarvis_v0.1.onnx`, `embedding_model.onnx`, `melspectrogram.onnx`
- Source: official openWakeWord v0.5.1 release
  https://github.com/dscripka/openWakeWord/releases/tag/v0.5.1
- License: upstream code is Apache-2.0; the **pretrained models are
  CC BY-NC-SA 4.0** (non-commercial). See
  https://github.com/dscripka/openWakeWord#license
- ⚠️ Confirm the upstream license yourself before commercial use.

## ASR model (sherpa-onnx Paraformer)

- Files: `sherpa-onnx-paraformer-zh-small-2024-03-09` (78 MB, default) or
  `sherpa-onnx-paraformer-zh-int8-2025-10-07` (228 MB, more accurate)
- Source: k2-fsa/sherpa-onnx `asr-models` release assets
  https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
- License: see the README/LICENSE shipped inside each model archive and
  the sherpa-onnx project (Apache-2.0 project; model licensing varies by
  origin — FunASR Paraformer models are published by Alibaba DAMO under
  their own terms; verify before commercial use).
