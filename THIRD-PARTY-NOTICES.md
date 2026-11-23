# Third-party notices

Handy Remote is distributed under the MIT licence (see [LICENSE](LICENSE)). It builds on the
following work. **Speech models are not part of this repository or its releases**: the server's
`setup.ps1` downloads them from their publishers onto your machine, and their own licences apply.
Check them before redistributing models yourself.

## Application

| Component                                                    | Licence                                                                | Source                            |
| ------------------------------------------------------------ | ---------------------------------------------------------------------- | --------------------------------- |
| Handy by CJ Pais (the upstream application)                  | MIT                                                                    | <https://github.com/cjpais/Handy> |
| Dependencies of the upstream app (Rust crates, npm packages) | Their respective licences (see `src-tauri/Cargo.lock`, `package.json`) | as upstream                       |

## Server software (installed by `setup.ps1` from PyPI)

| Component                                             | Licence                   | Source                                        |
| ----------------------------------------------------- | ------------------------- | --------------------------------------------- |
| OpenVINO                                              | Apache-2.0                | <https://github.com/openvinotoolkit/openvino> |
| Hugging Face Tokenizers                               | Apache-2.0                | <https://github.com/huggingface/tokenizers>   |
| NumPy                                                 | BSD-3-Clause              | <https://github.com/numpy/numpy>              |
| sherpa-onnx                                           | Apache-2.0                | <https://github.com/k2-fsa/sherpa-onnx>       |
| Other Python packages (see `server/requirements.txt`) | Their respective licences | PyPI                                          |

## Models (downloaded by the user)

| Model                                                                                                                             | Use                      | Licence                                                                                                                                                                                          | Source                                                                                            |
| --------------------------------------------------------------------------------------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| Qwen3-ASR 1.7B (Qwen team, Alibaba)                                                                                               | original model           | Apache-2.0                                                                                                                                                                                       | <https://huggingface.co/Qwen/Qwen3-ASR-1.7B>                                                      |
| Qwen3-ASR 1.7B, OpenVINO INT8 conversion                                                                                          | final text               | Apache-2.0 (as tagged on the model card)                                                                                                                                                         | <https://huggingface.co/dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO>                                    |
| Qwen3-ASR 0.6B (Qwen team, Alibaba)                                                                                               | original model           | Apache-2.0                                                                                                                                                                                       | <https://huggingface.co/Qwen/Qwen3-ASR-0.6B>                                                      |
| Qwen3-ASR 0.6B, OpenVINO NPU INT8 conversion                                                                                      | previews and drafts      | Apache-2.0 (as tagged on the model card)                                                                                                                                                         | <https://huggingface.co/iamwaqargulzar/handy-qwen3-asr-0.6b-openvino-npu-int8>                    |
| Silero VAD                                                                                                                        | voice activity detection | MIT                                                                                                                                                                                              | <https://github.com/snakers4/silero-vad>                                                          |
| Streaming Paraformer bilingual zh-en (FunASR / ModelScope, ONNX export by sherpa-onnx)                                            | fast CPU drafts          | Apache-2.0 on ModelScope; FunASR models are also covered by the [FunASR Model License](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE) (attribution); verify before redistribution | <https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-paraformer/paraformer-models.html> |
| CT-Transformer punctuation zh-en (FunASR / ModelScope `punc_ct-transformer_zh-cn-common-vocab272727`, ONNX export by sherpa-onnx) | punctuation              | Apache-2.0 on ModelScope; also see the FunASR Model License above; verify before redistribution                                                                                                  | <https://k2-fsa.github.io/sherpa/onnx/punctuation/pretrained_models.html>                         |

## Fonts, icons and other assets

As in upstream Handy; see that repository for details.
