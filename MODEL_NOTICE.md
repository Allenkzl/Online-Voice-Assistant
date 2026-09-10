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

## ASR model (sherpa-onnx, local)

Default — bilingual Chinese + English:

- Files: `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`
  (`model.int8.onnx` 228 MB + `tokens.txt`), unpacked to
  `models/asr_sense_voice_zh_en_int8`
- Languages: Chinese, English, Cantonese, Japanese, Korean
- Source / license: SenseVoiceSmall from FunAudioLLM, published under the
  **FunASR Model Open Source License Agreement v1.1** (free to use, modify and
  share — attribution and retention of the model name are required; read it at
  https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)

Optional fallbacks (Chinese only, kept for rollback):

- Files: `sherpa-onnx-paraformer-zh-small-2024-03-09` (78 MB, faster) or
  `sherpa-onnx-paraformer-zh-int8-2025-10-07` (228 MB, more accurate)
- Source: k2-fsa/sherpa-onnx `asr-models` release assets
  https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
- License: see the README/LICENSE shipped inside each model archive, plus the
  FunASR model license linked above (Paraformer models are published by
  Alibaba DAMO under their own terms; verify before commercial use).

Model selection, measured accuracy and latency: see
`docs/asr-bilingual-models-2026-09-10.md`.
