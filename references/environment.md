# 已验证本机环境

实测机器：Apple Silicon Mac，M1，16GB。持久环境位置为 `~/.codex/tools/film-voice`；Skill不重复复制模型。

| 用途 | 已实测组件 |
|---|---|
| 下载公开视频 | yt-dlp 2026.10.26 |
| 音频提取/裁剪/导出 | imageio-ffmpeg 0.7.0内置FFmpeg 7.1 |
| 分段和声纹 | sherpa-onnx 2.0.1；pyannote segmentation；3D-Speaker ERes2Net中文模型 |
| 辅助转写 | faster-whisper 1.2.1；small；CPU int8 |
| 人声分离 | audio-separator 0.48.0；默认 UVR-MDX-NET-Inst_HQ_3.onnx，可选 Kim_Vocal_2.onnx；Apple MPS/CoreML |
| 关键依赖 | numpy 2.5.4、soundfile 0.14.0、librosa 0.11.0、audioread 3.1.0、av 19.0.2、torch 2.15.0、onnxruntime 1.31.1 |

运行Python：`~/.codex/tools/film-voice/venv/bin/python`。

需要的模型位置：

```text
models/
├── segmentation.onnx
├── embedding.onnx
├── separator/UVR-MDX-NET-Inst_HQ_3.onnx
├── separator/Kim_Vocal_2.onnx（可选对比模型）
└── whisper/models--Systran--faster-whisper-small/snapshots/<revision>/
    ├── model.bin
    ├── config.json
    ├── tokenizer.json
    └── vocabulary.txt
```

## 已遇到的问题

- 默认shell PATH不完整：Python和脚本用绝对路径；FFmpeg由imageio-ffmpeg定位，分离器需要的bin路径由脚本配置。
- 系统工具路径必须在导入分离器之前配置，否则platform.processor可能读成空值并导致Apple加速未被识别。脚本入口已处理；无法启用加速时可在CPU运行，耗时会增加。
- av19与faster-whisper的文件解码调用不兼容：直接用soundfile读取16k单声道数组传给模型，已在脚本处理。不要改回让Whisper直接读MP4。
- Hugging Face代理环境缺socksio时，按模型名字加载会失败：直接使用已经下载的模型绝对路径。转写脚本也不在每次调用时联网检查模型。
- audio-separator依赖缺audioread，且曾遇到librosa版本兼容问题：当前安装audioread并固定librosa0.11.0，已跑通。不要无理由升级整套包。
- 只有ffmpeg没有ffprobe可能让yt-dlp提示警告。本工作流用PyAV检查音轨与时长，不把该警告自动视为下载失败。
- 分段模型会把同一角色拆成多个组；反之声音相似的人也可能混组。真实角色需参考声纹加剧情/画面核查。
- Mac上的pyJianYingDraft不支持一键自动导出，不作为本工作流的默认依赖。
- Kim_Vocal_2.onnx 已通过短样导出和清理流程验证；这只证明流程可运行，不代表对所有场景都比默认模型更干净。

## 环境缺失时

先运行doctor检查实际缺项，优先复用本机环境或缓存。需要迁移时新建独立venv，按上表版本安装匹配平台的包；大模型放运行环境models目录。不要把另一平台安装成功写成本机已验证。

模型下载从下列官方项目/模型页确认当前地址，避免猜测URL。网络不可用时保留输入与已完成结果，明确缺失环节，不将未分离原声命名为去配乐版。

- [sherpa-onnx项目与示例](https://github.com/k2-fsa/sherpa-onnx)
- [说话人分段模型说明](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)
- [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [small模型文件](https://huggingface.co/Systran/faster-whisper-small)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)

此环境只安装到用户工具目录；不要求修改系统Python，不包含云上传或外部消息发送步骤。
