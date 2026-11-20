# macOS 使用说明

macOS 版本沿用现有 FastAPI 服务和浏览器工作区。麦克风通过 CoreAudio 直接采集；由于 macOS 不向普通应用提供系统输出回环，面试官声音需要通过 BlackHole、Loopback 或 Background Music 等虚拟输入设备转接。默认配置使用 `BlackHole 2ch`。

## 环境要求

- macOS 12 或更新版本，Apple Silicon 与 Intel 均可
- Python 3.11 或更新版本，推荐 3.11 或 3.12
- 可用的 OpenAI-compatible STT `/audio/transcriptions` 服务
- 用于系统声音的 BlackHole 2ch 或等价虚拟音频设备

## 首次配置

1. 安装 BlackHole 2ch。已安装 Homebrew 时可执行：

   ```bash
   brew install --cask blackhole-2ch
   ```

   也可从 BlackHole 官方项目下载安装包。

2. 打开“音频 MIDI 设置”，点击左下角 `+`，选择“创建多输出设备”。同时勾选面试时使用的耳机/扬声器与 `BlackHole 2ch`。物理输出作为主设备时，为 BlackHole 开启“漂移校正”。

3. 在 macOS“系统设置 > 声音 > 输出”中选择刚创建的多输出设备。这样声音仍从耳机/扬声器播放，同时复制一份给 BlackHole。

4. 双击仓库根目录的 `run_macos.command`。首次启动会创建 `.venv`、安装 `audio`、`pdf` 与连续语音依赖、在缺少时从 `config.example.json` 创建本地 `config.json`，然后启动并打开 `http://127.0.0.1:8527`。

5. macOS 请求权限时允许启动 Python 的应用访问麦克风。双击启动时通常是 Terminal；从开发工具启动时可能显示该开发工具的名称。可在“系统设置 > 隐私与安全性 > 麦克风”中重新检查。

网页只在点击“开始监听”后采集，不会因打开页面自动录音。

## 设备选择

- “面试官 · 系统声音输入”应选择 BlackHole 或其他虚拟输入。
- “我 · 麦克风输入”选择 Mac 内置麦克风、耳机麦克风或外接声卡。
- `audio.system_device_name` 设置系统声音的默认设备名，默认是 `BlackHole 2ch`。
- `audio.mic_device_name` 设置麦克风默认设备名；留空使用 macOS 默认输入。

如果页面提示未发现系统声音输入，先确认虚拟设备已安装，再点击“刷新设备”或重启服务。多输出设备只负责路由；网页选择的是其中的 BlackHole 输入，不是多输出设备本身。

## 连续语音与会话录音

兼容流程和连续语音流程共用同一套 CoreAudio 设备选择、录音归档和启动/停止生命周期。连续语音不会把处理窗口误当成问题结束，但启用前需要显式准备经过校验的本地 VAD 与端点模型：

```bash
.venv/bin/python scripts/prepare_speech_models.py
```

该命令会联网下载两个固定版本的 ONNX 模型并校验 SHA-256；正常启动不会自动下载模型。准备完成后，暂停采集，在网页“语音流程”中选择“连续语音 · 试用”。

会话录音会保存 CoreAudio 的系统声音与麦克风原始 PCM。结束面试后的离线精转写是独立可选能力；需要时安装 `.venv/bin/python -m pip install -e '.[offline-asr]'` 并提前准备 faster-whisper 权重。应用不会在录音或服务启动时静默下载大模型。

## 开发启动

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[audio,pdf,speech,dev]'
cp config.example.json config.json  # 仅在本地配置不存在时
.venv/bin/python server.py
```

## 当前边界

- 系统声音依赖第三方虚拟音频驱动；项目不会静默安装或修改系统音频路由。
- 根目录 `config.json`、`data/` 与 `.env` 仍沿用 legacy 运行时位置并受 Git 忽略。本次适配没有迁移 Windows 用户数据。
- 网页保存的长期 API Key 目前只有 Windows DPAPI 保护。macOS 应使用会话密钥或由用户显式设置的环境变量，原生 Keychain 支持尚未实现。
- 实时 STT 服务由用户自行提供；启动器只检查配置中的 `/models` 是否可访问。连续语音和离线精转写模型均只通过上述显式命令准备。
