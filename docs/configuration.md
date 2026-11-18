# 配置与迁移

本文说明当前 legacy 实现实际读取的配置、公开模板的安全边界，以及迁移到 v2 本地配置时的处理方式。公开仓库只提供 `config.example.json`；`config.json` 始终是本地私有文件，不得提交。

## 安全边界

- 不要把真实 API key 写入 JSON。`api_key_env` 和 `fallback_api_key_env` 保存的是**环境变量名称**，不是密钥值。
- `resume_path`、`paper_paths`、`material_paths` 在公开模板中保持为空。只在本地 `config.json` 中填写自己的路径。
- 模板中的 `api.example.invalid` 和模型名都是不可用的保留占位值，必须在本地替换为用户自己选择的 OpenAI-compatible provider 和模型。
- localhost STT 需要本机独立的兼容 `/audio/transcriptions` 服务；启动器可按 `stt.server_dir` 或 `INTERVIEW_COPILOT_STT_DIR` 启动既有的 `stt_server.py`，不会安装或下载 STT 模型。
- 不要提交 `.env`、`config.json`、`*.local.json`，也不要把配置或私有材料复制进 `docs/`。

## 创建本地配置

首次配置时复制模板为 `config.json`，只编辑复制出来的本地文件。已有可用的 `config.json` 时不要用模板覆盖它；按下方映射逐项迁移，并先在仓库外保留私有备份。

```bat
copy config.example.json config.json
```

模板本身不能直接连接真实 LLM provider，因为外部 URL 和模型均为安全占位值。

## 网页配置回答服务

电脑工作区的“模型设置”打开 `/settings`，可配置 OpenAI 兼容主服务、API 地址、API Key、快速/深入模型及备用服务。Provider 预设填入常用地址和模型名，实际可用模型以当前账号的“获取模型”或服务控制台为准。此页只配置回答模型，语音转写仍使用 `stt.*`。

- “获取模型”请求所选服务的 `/models`；“测试连接”向 `/chat/completions` 发送固定 `Reply with OK.`，输出上限 16 token。测试不会保存配置，也不附带简历、资料或对话；真实供应商可能计费。
- “保存配置”持久化地址、模型和开关，后续新任务使用新客户端；已开始的回答继续使用原地址和原凭据。
- 新密钥默认由 Windows DPAPI 按当前用户加密后写入 `data/provider_settings.db`。该目录被 Git 忽略，不能把数据库作为公开配置交付或跨用户复制。
- 取消“新密钥在本机加密保存”后，新输入的密钥只在当前进程生效；旧持久密钥会删除，重启后需重新输入。
- 密钥输入框留空保留现有密钥，清除按钮会标记删除，点击保存后生效。页面和 API 仅返回密钥是否存在与来源，不回显密钥；前端不写入 localStorage 或 URL。
- 更换服务地址会取消使用原地址的密钥，包括环境变量回退；应重新填写对应地址的密钥，或显式勾选“无需 API Key”。这避免把旧密钥发送到新服务。
- 网页覆盖优先于 `config.json` 的同名回答配置；未覆盖字段保留本地配置。没有网页密钥且未显式清除/更换地址时，保持现有环境变量查找顺序。网页不会修改环境变量。
- 备用服务只有启用后参与回答回退。测试备用服务只测试备用通道，连接测试本身不进行主备回退。
- 多窗口通过版本号防止覆盖，冲突时保留未保存输入并提示刷新。无法解密的保存密钥显示错误来源，需重新输入或清除。

此入口与 `/api/providers*` 仅允许本机电脑管理端访问，拒绝跨站请求，响应不缓存。独立手机端不提供设置 API。

离线预览明确显示“临时配置 / 模拟连接”，不使用真实 Provider；预览中的连接通过不代表真实网络或模型连通。

## 本人发言上下文

电脑页面默认选中系统默认麦克风，点击“监听”才开始采集；可选择关闭或指定设备，监听中也能切换。启动服务的 `--auto-listen` 仍按本地 `audio.*_device_index` 启动双通道，与网页初始选择独立。

本人转写不触发新题，上一题回答进入下一题的快速上下文，当前题的深挖会参考上一题及当前发言。上下文有字符预算，超长发言保留最近部分；开始新会话清空缓冲。AI 建议和本人真实转写使用不同来源字段，建议不会被记成已说内容。现阶段这些链路已通过合成转写测试，真实设备、STT 准确性和说话时间边界仍需实机验证。

## 字段映射

当前迁移保持字段路径不变：旧 `config.json` 中的有效字段迁入 v2 本地配置的同名位置。表中的“读取方”来自当前源码的静态对照；v2 settings 层接管前，legacy 仍直接读取根目录 `config.json`。

| 旧配置字段 | v2 本地字段 | 读取方 / 语义 | 迁移说明 |
| --- | --- | --- | --- |
| `resume_path` | `resume_path` | `server.py`；显式简历路径 | 保持字符串；公开模板必须为空 |
| `paper_paths` | `paper_paths` | `copilot/knowledge.py`；论文 PDF 列表 | 保持字符串数组；只在本地填写 |
| `material_paths` | `material_paths` | `copilot/materials.py`；辅助材料列表 | 保持字符串数组；只在本地填写 |
| `corpus.sources` | 同名 | `services/corpus.py`；本地笔记目录列表 | 每项为 `label`、`path`；显式空数组关闭目录扫描 |
| `corpus.sources_file` | 同名 | 目录清单 JSON；未设置 `sources` 时读取 | 默认 `data/corpus_sources.json`，仅在本地保存真实路径 |
| `corpus.quick_budget_chars` / `detail_budget_chars` | 同名 | 证据 JSON 的字符预算 | 默认 2400 / 4000，含证据元数据，不含简历、JD 和对话 |
| `host` | 兼容保留 | `server.py` 的当前启动器强制管理入口为 `127.0.0.1` | 手机使用独立的 `mobile.host`，不要通过管理入口开放局域网 |
| `port` | `port` | `server.py`；电脑管理端口 | 默认 `8527`；批处理启动器读取此字段 |
| `mobile.enabled` | 同名 | 是否启用独立手机监听器 | 默认 false；`--mobile` 可单次启用 |
| `mobile.host` / `mobile.port` | 同名 | 手机监听地址与端口 | 默认 `0.0.0.0:8528`；可绑定指定局域网 IP |
| `audio.loopback_device_index` / `mic_device_index` | 同名 | 后台自动采集的本机设备编号 | 可选；省略时使用系统默认输出回环/输入设备；设备变化后需重新核对编号 |
| `merge.wait_ms` | `merge.wait_ms` | `server.py`；题目分段等待时间 | 保持整数毫秒 |
| `merge.continuation_ms` | `merge.continuation_ms` | `server.py`；已出题后的续段合并窗口 | `0` 表示关闭 |
| `stt.url` | `stt.url` | `server.py` → `AudioEngine`；STT base URL | 当前应用使用键名 `url`，不要改成 `base_url`；localhost 示例保留 `/v1` |
| `stt.model` | `stt.model` | `server.py` → `STTClient`；转写模型 | 使用 STT 服务实际支持的模型名 |
| `stt.server_dir` | 同名 | `scripts/launch.ps1`；可选的本地 STT 项目目录 | 包含 `stt_server.py` 和 `venv/Scripts/python.exe`；环境变量 `INTERVIEW_COPILOT_STT_DIR` 优先，仅在 STT URL 为回环地址时用于自启 |
| `stt.api_key_env` | `stt.api_key_env` | `server.py` → `STTClient`；依次查找密钥 | 填环境变量名数组；本地无鉴权 STT 可用空数组 |
| `llm.base_url` | `llm.base_url` | `copilot/llm.py`；主 OpenAI-compatible endpoint | 将模板占位 URL 替换为自己的 provider URL |
| `llm.quick_model` | `llm.quick_model` | `copilot/llm.py`；快速回答模型 | 填 provider 实际模型标识；为空时进入通用模型回退逻辑 |
| `llm.detail_model` | `llm.detail_model` | `copilot/llm.py`；深度回答模型 | 填 provider 实际模型标识 |
| `llm.model` | `llm.model` | `copilot/llm.py`；通用模型回退 | 建议显式填写，避免快速/深度模型为空时得到空模型名 |
| `llm.api_key_env` | `llm.api_key_env` | `copilot/llm.py`；主 provider 密钥查找顺序 | 只写环境变量名，不写真实 key |
| `llm.disable_thinking` | `llm.disable_thinking` | `copilot/llm.py`；兼容 provider 的思考开关 | 布尔值；只有当前实现识别的兼容 endpoint 会附加参数 |
| `llm.temperature` | `llm.temperature` | `copilot/llm.py`；采样温度 | 数值 |
| `llm.max_tokens` | `llm.max_tokens` | `copilot/llm.py`；最大输出 token | 整数 |
| `llm.quick_max_tokens` / `detail_max_tokens` | 同名 | 分层输出上限，覆盖通用 `max_tokens` | 未设置时保留原上限；示例 900 / 2400，备用尝试沿用该层预算 |
| `llm.quick_context_chars` / `detail_context_chars` | 同名 | 完整输入消息序列化后的字符上限 | 默认 16000 / 24000；包含系统规则、资料 JSON、历史、当前问题和方向，按字符而非精确 token 计数 |
| `llm.provider_options` | 同名 | 主通道的 provider 参数 | 仅转发 `thinking`、`enable_thinking`、`reasoning_effort`；必须按实际端点测试，默认空对象 |
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
| `audio.merge_quiet_ms` | 删除 | 当前源码没有读取该字段；题目合并使用 `merge.wait_ms`、`merge.smart_continuation_ms` 和 `merge.incomplete_wait_ms` |
| `merge.continuation_ms` | 已弃用，不再读取 | 旧版时间窗口内无条件拼接会串题；新版只在内容提示属于补充时使用 `merge.smart_continuation_ms` |
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

- 当前 `server.py` 在 lifespan 启动时读取配置并装配资源，导入模块不启动采集或打开数据库。缺失必需配置仍会使启动失败。
- `启动面试辅助.bat` 和 `后台手机模式.bat` 使用 `scripts/launch.ps1`，读取配置端口并检查主服务身份和就绪状态。STT URL 为本机时，无论主服务是否已运行，都会检查 STT `/models`；不可用时按环境变量 `INTERVIEW_COPILOT_STT_DIR` 或 `stt.server_dir` 启动已有服务并等待 API 就绪。目录未配置时需自行启动 STT；STT 未就绪时电脑模式提示仍可手动提问，手机模式会拒绝自动采集。
- STT 的 `localhost`、`127.0.0.0/8` 和 `::1` 请求绕过环境与 Windows 系统代理，防止本机服务被代理成 502；远端 STT 继续保留原代理策略。启动器管理本机服务的 HTTP 请求也直接访问回环。`/models` 可用只证明 API 已启动，不代表模型已完成加载或真实转写验收。
- `stt.url` 是应用配置键，而 `STTClient` 构造参数内部叫 `base_url`。这是现有适配关系，不是两个都要配置。

## Obsidian 与本地笔记

笔记作为按题检索的参考资料，不是训练模型参数，也不自动成为本人经历证据。目录扫描支持 `.md`、`.markdown`、`.txt`；已有简历、PDF 论文和上传材料仍走原有解析，再进入同一个检索索引。扫描不会改动原文件，不为每篇笔记调用摘要模型。

当 `corpus.sources` 未显式设置时，应用读取本地 `data/corpus_sources.json`：

```json
{"sources": [{"label": "notes", "path": "D:/your-vault"}]}
```

`label` 为唯一的 ASCII 字母、数字、下划线或连字符标识，最多 40 字符；模型收到 `notes:relative/path.md`，不会收到目录绝对路径。支持多目录；不扫描整个电脑。公开模板的 `sources: []` 明确关闭目录扫描；使用本地清单时移除该字段，或将真实目录直接配置在本地 `config.json`。

索引使用内存 SQLite FTS5/BM25；服务启动时在受管理的后台工作线程构建，关闭时释放。笔记更新、删除或修改目录清单后，当前版本需重启服务重建；尚无文件监视和专用刷新页面。同步失败在状态事件提示，`GET /api/health` 的 `corpus` 包含就绪状态、文件/片段计数和失败/跳过数。

默认排除隐藏目录、`.obsidian`、`.git`、`.trash`、`node_modules`、符号链接、越界文件、超过 2 MB 的单文件及首行引用块明确标为作废/弃用的稿件。YAML frontmatter 不作为正文检索；不从其中猜测作者或确认状态。导航片段会被过滤，但检索排序和版本冲突判断仍可能不准确。仅常见的 key/Bearer 模式会被遮盖，不能视为完备的敏感信息检测器。

证据保留相对来源、章节、文本行号与稳定标识；PDF 的行号指提取文本，不是原 PDF 页码。目录笔记和辅助材料均标为 `reference`，个人角色和成果仍需简历或明确本人陈述支持。证据进入真实模型请求时，选中的片段会发送给配置的 provider；不是整库上传。本轮实际联网评测仅使用合成内容。

本地检查默认只输出来源定位与计数；`--show-excerpts` 才输出正文：

```powershell
.\venv\Scripts\python.exe scripts\inspect_corpus.py --query 'Agent 和 LLM 的区别'
.\venv\Scripts\python.exe scripts\evaluate_answers.py --validate-only
```

真实模型评测需显式提供 endpoint/model 和进程密钥。`evaluate_answers.py` 默认只跑三个合成案例，可重复 `--case T02 --case E02` 选择。`--disable-thinking` 仅发送 `thinking.type=disabled`，目前只在本轮的兼容端点与 DeepSeek 别名验证，不能假设其他 provider 支持。同一轮修改了 Prompt、预算和推理选项时，不能把速度变化归因于单一参数。

统一输入预算会优先保留完整当前问题及深挖方向，再对简历、真实口述、检索证据和历史分配空间。被节选或省略的字段带 `context_coverage.partial_fields` 标记，不能据此判断事实不存在；证据缩短时同步收紧行号范围。若仅必需内容就已超限，会在请求 provider 之前报错，不静默截断问题。字符上限不能替代 provider 的 token 上限，需按实际模型留出输出空间。

没有前文和本人口述时，少数明确缺少指代的短问（如“为什么这样做？”）直接返回一项澄清，尝试标为 `local:clarification`，不调用模型。简历或检索命中不会被用来猜测此类指代。有前文、有本人发言或问题本身带具体技术对象时继续由模型作答。

检索遇到明确英文技术词且片段正文或末级标题有命中时，优先限定在这些片段，减少同篇笔记的无关章节；找不到技术词命中则保留原有词法回退。该策略仍可能漏掉只用同义表达的相关片段，引用本身也不保证资料技术结论正确。

评测可用 `--prompt-set tests/evaluation/prompts/m3-2.json` 对照旧提示词，`--all` 显式运行全部 50 条合成案例。`--enable-thinking` 与 `--disable-thinking` 互斥；同一端点也需单独验证支持情况。比较结论需同时检查实际消息、案例与提示词哈希；只替换提示词不等于恢复整套旧检索和旧上下文链路。

M3-4 增加 Answer Document v1，接口和事实检查语义见 [结构说明](answer-document-v1.md)。升级环境时安装 `pyproject.toml` 中的 `markdown-it-py` 依赖（正常安装项目会一起安装）；页面模块为本地 ES Module，无需前端构建或 CDN。M4-2 将新完成回答的来源快照写入历史库，`/history` 支持搜索、复盘和导出；原有回答字符串及数据库表保留。首次打开旧库前自动创建 SQLite 一致性备份，再新增 `answer_documents` 表；备份位置与回退方法见 [历史与升级说明](history-review.md)。旧记录没有来源时不会补造来源，旧版本已经截断的文字无法恢复。

M4-1 增加 `qrcode` 依赖和本地 Lucide 图标。普通桌面启动不自动采集；`--auto-listen` 在服务 lifespan 中启动电脑的两个音频通道，不依赖页面。手机监听器只提供显示、配对和退出配对，不共享电脑的完整静态目录、管理 API 或背景材料快照。具体配置、配对有效期和验证范围见 [手机显示说明](mobile-display.md)。

M4-3 增加电脑 `/files` 资料库，新上传和公开 GitHub Markdown 经本地解析、概览、分块及索引后参与按题检索。原文件、处理状态和开关保存于 `data/documents/`，无需新增配置；PDF 仍使用现有 `pdf` 安装依赖。GitHub 导入遵循进程的代理配置，不使用模型 key 或自动读取仓库 token。已有简历/论文/材料保留原链路，没有自动迁移原文件。类型、限额和验证范围见 [资料库说明](document-library.md)。
## 资料理解与语义检索

`knowledge.enabled` 默认 `true`：正文就绪后调用当前回答 Provider 生成摘要、事实与项目记录，分段正文会发送给该服务。`knowledge.semantic_enabled` 默认 `true`，`knowledge.semantic_model` 默认为 `BAAI/bge-m3`，也可填写本地 SentenceTransformer 模型目录。

本地语义依赖可通过 `venv\Scripts\python.exe -m pip install -e ".[semantic]"` 安装。模型需提前缓存；启动只加载本机模型，不自动下载。向量在 CPU 上生成并持久化复用，后台预热完成后显示就绪。依赖、模型或推理失败时，资料正文和关键词检索仍可用，详情显示语义索引错误。

关闭结构化分析可设 `knowledge.enabled=false`；只关闭向量检索可设 `knowledge.semantic_enabled=false`。修改这些本地配置后重启服务。卡片核对决定不会因关闭功能而删除，资料目录保持受 Git 忽略保护。

验证工具：`venv\Scripts\python.exe scripts\validate_knowledge.py --retrieval-only`。脚本复用或更新本地向量缓存，不采集音频，不修改核对决定或面试历史；结果默认保存到受忽略保护的 `data/`。去掉该参数会使用当前真实 Provider 对固定问题生成旧/新上下文对照，可能产生 API 费用；可用 `--question-index 4` 聚焦单题。

## 通用作答、语音补充与诊断

通用技术题和方案题允许使用当前模型自身的知识；资料只提供相关依据，通用题不注入整份简历。个人经历仍需资料或本人发言支持，人工核对约束继续保留。回答优先给操作顺序、失败处理及关键取舍，快速层目标 150 至 280 字，复杂题可能更长；这是提示词目标，不是硬截断。

模型可结合最近对话还原近音、漏词和指代，原始转写不会被覆盖。上下文足够明确时直接回答；关键数字、否定词或约束存在多种解释时再澄清。该行为依赖模型判断，不等于转写准确率已提高。

`merge.wait_ms` 默认 1100；疑似未完成句使用 `merge.incomplete_wait_ms`，默认 4000。成题后默认 8000 毫秒内收到有补充线索的片段，取消旧生成，在同一题上合并重答，桌面、手机和历史同步替换；显式换题或候选人已开始实质回答时关闭续接。`merge.smart_continuation_ms=0` 关闭成题后续接。超过窗口的补充可能成为新题，启发式也可能判断错误。

`audio.pre_roll_ms` 默认 300，保留触发语音阈值前的短缓冲，减轻起音截断。纯致谢、试音、咳嗽标注和重复文本不触发自动作答；短模式重复转写记录为 `stt_rejected`。正常转写事件保存片段时长与电平供排查，不默认保存现场音频。

`venv\Scripts\python.exe scripts\validate_stt.py --output data/stt-quality.json` 对已知 WAV、静音、白噪声和低频声进行有/无热词的 10 项真实 STT 对照，不采环境音。默认 WAV 是本机已有的 `data/stt-synthetic-check.wav`，其他机器需通过 `--speech-wav` 与 `--expected` 指定。外部 Whisper 服务的本机解码调整与回退位置见 [本轮报告](2026-11-18-knowledge-speech-answer-report.md)。
