# 配置与迁移

本文说明当前 legacy 实现实际读取的配置、公开模板的安全边界，以及迁移到 v2 本地配置时的处理方式。公开仓库只提供 `config.example.json`；`config.json` 始终是本地私有文件，不得提交。

## 安全边界

- 不要把真实 API key 写入 JSON。`api_key_env` 和 `fallback_api_key_env` 保存的是**环境变量名称**，不是密钥值。
- `resume_path`、`paper_paths`、`material_paths` 在公开模板中保持为空。只在本地 `config.json` 中填写自己的路径。
- 模板中的 `api.example.invalid` 和模型名都是不可用的保留占位值，必须在本地替换为用户自己选择的 OpenAI-compatible provider 和模型。
- localhost STT 示例假设本机已有兼容 `/audio/transcriptions` 的服务；仓库不会替用户启动或配置该服务。
- 不要提交 `.env`、`config.json`、`*.local.json`，也不要把配置或私有材料复制进 `docs/`。

## 创建本地配置

首次配置时复制模板为 `config.json`，只编辑复制出来的本地文件。已有可用的 `config.json` 时不要用模板覆盖它；按下方映射逐项迁移，并先在仓库外保留私有备份。

```bat
copy config.example.json config.json
```

模板本身不能直接连接真实 LLM provider，因为外部 URL 和模型均为安全占位值。

## 字段映射

当前迁移保持字段路径不变：旧 `config.json` 中的有效字段迁入 v2 本地配置的同名位置。表中的“读取方”来自当前源码的静态对照；v2 settings 层接管前，legacy 仍直接读取根目录 `config.json`。

| 旧配置字段 | v2 本地字段 | 读取方 / 语义 | 迁移说明 |
| --- | --- | --- | --- |
| `resume_path` | `resume_path` | `server.py`；显式简历路径 | 保持字符串；公开模板必须为空 |
| `paper_paths` | `paper_paths` | `copilot/knowledge.py`；论文 PDF 列表 | 保持字符串数组；只在本地填写 |
| `material_paths` | `material_paths` | `copilot/materials.py`；辅助材料列表 | 保持字符串数组；只在本地填写 |
| `host` | `host` | `server.py`；HTTP 监听地址 | 默认仅绑定 `127.0.0.1`；暴露到局域网前自行评估风险 |
| `port` | `port` | `server.py`；HTTP 监听端口 | 保持整数；当前批处理启动器仍固定检查并打开 `8527` |
| `merge.wait_ms` | `merge.wait_ms` | `server.py`；题目分段等待时间 | 保持整数毫秒 |
| `merge.continuation_ms` | `merge.continuation_ms` | `server.py`；已出题后的续段合并窗口 | `0` 表示关闭 |
| `stt.url` | `stt.url` | `server.py` → `AudioEngine`；STT base URL | 当前应用使用键名 `url`，不要改成 `base_url`；localhost 示例保留 `/v1` |
| `stt.model` | `stt.model` | `server.py` → `STTClient`；转写模型 | 使用 STT 服务实际支持的模型名 |
| `stt.api_key_env` | `stt.api_key_env` | `server.py` → `STTClient`；依次查找密钥 | 填环境变量名数组；本地无鉴权 STT 可用空数组 |
| `llm.base_url` | `llm.base_url` | `copilot/llm.py`；主 OpenAI-compatible endpoint | 将模板占位 URL 替换为自己的 provider URL |
| `llm.quick_model` | `llm.quick_model` | `copilot/llm.py`；快速回答模型 | 填 provider 实际模型标识；为空时进入通用模型回退逻辑 |
| `llm.detail_model` | `llm.detail_model` | `copilot/llm.py`；深度回答模型 | 填 provider 实际模型标识 |
| `llm.model` | `llm.model` | `copilot/llm.py`；通用模型回退 | 建议显式填写，避免快速/深度模型为空时得到空模型名 |
| `llm.api_key_env` | `llm.api_key_env` | `copilot/llm.py`；主 provider 密钥查找顺序 | 只写环境变量名，不写真实 key |
| `llm.disable_thinking` | `llm.disable_thinking` | `copilot/llm.py`；兼容 provider 的思考开关 | 布尔值；只有当前实现识别的兼容 endpoint 会附加参数 |
| `llm.temperature` | `llm.temperature` | `copilot/llm.py`；采样温度 | 数值 |
| `llm.max_tokens` | `llm.max_tokens` | `copilot/llm.py`；最大输出 token | 整数 |
| `llm.fallback_base_url` | `llm.fallback_base_url` | `copilot/llm.py`；备用 endpoint | 不使用备用通道时可留空 |
| `llm.fallback_model` | `llm.fallback_model` | `copilot/llm.py`；备用模型 | 与备用 endpoint 配套填写 |
| `llm.fallback_api_key_env` | `llm.fallback_api_key_env` | `copilot/llm.py`；备用密钥查找顺序 | 不使用备用通道时设为空数组 |
| `audio.sample_rate` | `audio.sample_rate` | `copilot/audio.py`；目标采样率 | 当前处理链按整数读取 |
| `audio.frame_ms` | `audio.frame_ms` | `copilot/audio.py`；VAD 帧长 | 整数毫秒 |
| `audio.silence_end_ms` | `audio.silence_end_ms` | `copilot/audio.py`；静音结束阈值 | 整数毫秒 |
| `audio.min_segment_ms` | `audio.min_segment_ms` | `copilot/audio.py`；最短片段 | 整数毫秒 |
| `audio.max_segment_ms` | `audio.max_segment_ms` | `copilot/audio.py`；最长片段 | 整数毫秒 |
| `audio.speech_threshold_offset_db` | `audio.speech_threshold_offset_db` | `copilot/audio.py`；相对噪声底阈值 | 数值 dB |
| `audio.absolute_speech_db` | `audio.absolute_speech_db` | `copilot/audio.py`；绝对语音阈值 | 数值 dBFS |

### 旧字段的特殊处理

| 旧字段 | 处理方式 | 原因 |
| --- | --- | --- |
| `stt.api_key` | 不迁入公开模板；将真实值移到用户显式设置的环境变量，并把变量名写入 `stt.api_key_env` | 当前 legacy 接口兼容内联值，但公开配置必须避免明文 key |
| `stt.timeout` | 暂不迁移 | 通用 `STTClient` 能读取该键，但当前应用从 `server.py` 创建 `AudioEngine` 时没有传递它，实际 HTTP timeout 仍由实现内部固定 |
| `audio.merge_quiet_ms` | 删除 | 当前源码没有读取该字段；题目合并使用 `merge.wait_ms` 和 `merge.continuation_ms` |
| 其他未知字段 | 暂时保留在私有备份，不直接带入 v2 | 先确认读取方和类型，避免把历史私有值或失效开关扩散到新配置 |

## 环境变量规则

v2 只读取启动进程中**由用户显式设置**的环境变量。v2 不搜索、不加载 Hermes 的私有 `.env`，也不应从用户主目录或其他工具目录继承配置文件内容。

例如，模板把主密钥变量命名为 `LLM_API_KEY`。用户应在启动应用的同一 shell、服务配置或操作系统凭据管理流程中设置它；文档和 JSON 中都不应出现真实值。

```bat
set LLM_API_KEY=<set-locally-do-not-commit>
python server.py
```

当前 legacy `copilot/llm.py` 仍会尝试读取项目根目录和用户主目录的 `.env`，这是兼容期行为，不是 v2 契约。迁移时应显式设置进程环境；不要依赖 Hermes 或其他工具维护的私有 `.env`。本任务不修改 legacy 加载逻辑，以保证旧版继续可用。

## 建议迁移顺序

1. 不修改现有可用的 `config.json`；先在仓库外创建私有备份。
2. 以 `config.example.json` 为字段清单，对照上表把仍有效的值逐项转入本地配置。
3. 将任何内联 STT key 改为显式环境变量，并在 JSON 中只保留变量名。
4. 用自己的 provider URL 和模型替换 `.invalid` 占位值；不使用备用通道时清空三个 `fallback_*` 字段。
5. 删除 `audio.merge_quiet_ms`；不要把未接线的 `stt.timeout` 当作有效调优项。
6. 保留 `config.json` 在忽略列表中。提交前只检查 `config.example.json` 和文档，不输出或复制本地配置内容。

## 当前兼容性注意事项

- legacy 模块在 import 时读取 `config.json`；缺失必需段或必需键会直接失败。因此迁移应一次完成完整结构，不要边删边运行。
- `启动面试辅助.bat` 当前固定访问 `127.0.0.1:8527` 和 localhost STT 地址。若本地修改 `host`、`port` 或 STT URL，需要自行同步启动方式；本次模板/文档任务不改 legacy 启动器。
- `stt.url` 是应用配置键，而 `STTClient` 构造参数内部叫 `base_url`。这是现有适配关系，不是两个都要配置。
