# 模型来源

文件来自 openWakeWord 官方 v0.5.1 release，与 openWakeWord 0.6.0 配合使用：

- https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.onnx
- https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx
- https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx

模型二进制不纳入 Git；SHA256SUMS 记录本次官方直连下载内容，部署后使用 sha256sum -c 校验传输一致性。

上游说明代码为 Apache-2.0，包含的预训练模型为 CC BY-NC-SA 4.0。商业展厅使用需单独确认模型授权。参见 https://github.com/dscripka/openWakeWord#license 。

流程参考 openvoicestream 的 OpenWakeWordSource（提交 68ba08e54b47edd1d3aad57c9feba9095400052d），本项目独立实现 ALSA 采集、检测、应答，不引入机械臂应用或语音服务器。
