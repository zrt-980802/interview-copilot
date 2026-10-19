# Interview Copilot v2 渐进式重整实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 在不破坏旧版可用性的前提下，建立 Windows-first、跨平台可扩展的 v2 后端和模块化浏览器前端，逐条迁移现有功能并完成开源隔离。

**Architecture:** 使用 `src/interview_copilot` 新建无导入副作用的 v2 分层架构；通过 legacy adapter 渐进迁移音频、STT、会话、LLM、知识库和历史持久化。前端采用原生 ES Modules，在 `/v2` 独立运行，达到功能等价后再切换默认入口。

**Tech Stack:** Python 3.11+、FastAPI、httpx、SQLite、PyAudioWPatch（Windows 可选依赖）、原生 HTML/CSS/ES Modules。

**Design spec:** `docs/superpowers/specs/2026-09-23-v2-incremental-architecture-design.md`

**重要约束：**

- 当前项目不是 Git 仓库。以下“提交”步骤须在完成仓库初始化并审查首个提交内容后执行；在此之前用阶段性变更清单代替提交。
- 不删除原始个人数据、历史数据库或旧版源码。
- 结构迁移阶段冻结 Prompt、VAD 参数、模型选择和视觉样式。
- 用户已要求先从代码层面重整；开始实际实施前仍需按任务顺序执行，不跳到真实设备/API 测试。

---

## Phase 0：开源隔离和工作区边界

### Task 1：建立发布清单与敏感数据清单

**Objective:** 明确哪些文件属于源码、配置模板、运行数据、缓存、个人材料和历史产物。

**Files:**
- Create: `docs/release-boundary.md`
- Create: `docs/private-data-inventory.md`
- Inspect: `config.json`, `data/`, `venv/`, `__pycache__/`, `diag_*.py`, `test_*.py`

**Steps:**

1. 用只读文件清单记录项目根目录和 `data/` 的文件类型。
2. 在 `private-data-inventory.md` 中记录个人简历、论文、JD、SQLite/WAL/SHM、材料缓存和路径元数据；不复制具体敏感内容。
3. 在 `release-boundary.md` 定义“可提交”“模板”“运行生成”“永不提交”四类。
4. 核对 `.gitignore` 覆盖所有“永不提交”路径。
5. 输出待人工确认的发布候选清单。

**Verification:** 不运行应用；仅确认文档中的每类路径可在工作区找到，且没有把敏感正文写入文档。

### Task 2：完善配置模板和配置迁移说明

**Objective:** 公开配置不包含任何个人路径、真实 provider、真实 key 或私人材料。

**Files:**
- Modify: `config.example.json`
- Create: `docs/configuration.md`
- Keep local-only: `config.json`

**Steps:**

1. 审核 `config.example.json` 字段与当前实现的实际读取字段一致。
2. 将 provider 示例改为通用占位 URL/模型；保留 localhost STT 示例。
3. 记录从旧 `config.json` 迁移到本地配置的字段映射。
4. 明确环境变量只由用户显式设置，v2 不读取 Hermes 私有 `.env`。
5. 不修改现有本地 `config.json`，避免破坏旧版。

**Verification:** 静态 JSON 解析和字段对照；不启动服务。

### Task 3：确定许可证与第三方边界

**Objective:** 在用户选择许可证后补齐项目许可和第三方声明。

**Files:**
- Create after user decision: `LICENSE`
- Create: `THIRD_PARTY_NOTICES.md`

**Steps:**

1. 向用户确认 MIT、Apache-2.0 或其他许可证。
2. 枚举核心依赖及许可证来源。
3. 检查 PyAudioWPatch、FastAPI、httpx、pypdf 等是否与目标许可证兼容。
4. 写入 LICENSE 和第三方声明。

**Verification:** 人工核对许可证文本来源；不自行编造许可证条款。

---

## Phase 1：v2 无副作用骨架

### Task 4：建立 Python src layout 和最小打包配置

**Objective:** 创建 v2 包结构，不引用 legacy，不改变旧入口。

**Files:**
- Create: `pyproject.toml`
- Create: `src/interview_copilot/__init__.py`
- Create: `src/interview_copilot/app.py`
- Create: `src/interview_copilot/settings.py`
- Create: `src/interview_copilot/lifecycle.py`
- Create: `tests/v2/test_import_side_effects.py`

**Steps:**

1. 写入 `pyproject.toml` 的 Python 版本、核心依赖和可选 extras：`audio`, `pdf`, `dev`。
2. 写一个导入副作用测试：在空临时目录导入包，断言没有创建 DB、data 目录或后台线程。
3. 运行该测试，预期在包尚未实现时失败。
4. 创建无副作用的最小 v2 包和 `create_app(settings)` 工厂。
5. 再运行测试，预期通过。
6. 旧 `server.py` 保持不变。

**Verification command:** `python -m pytest tests/v2/test_import_side_effects.py -v`

### Task 5：实现不可变 Settings

**Objective:** 将配置解析和校验从业务模块中抽离。

**Files:**
- Modify: `src/interview_copilot/settings.py`
- Create: `tests/v2/test_settings.py`

**Steps:**

1. 为缺失配置、本地配置覆盖、环境变量覆盖分别写失败测试。
2. 定义冻结 dataclass：AppSettings、AudioSettings、STTSettings、LLMSettings、StorageSettings。
3. 实现 `load_settings(path=None, env=None)`；不在 import 时调用。
4. 校验 localhost 默认 host、正整数端口、非负 timeout、路径展开。
5. 禁止自动扫描 Desktop 或 Hermes `.env`。
6. 运行 Settings 测试。

**Verification command:** `python -m pytest tests/v2/test_settings.py -v`

### Task 6：建立领域模型和事件协议

**Objective:** 定义跨输入、转写、会话、API 和前端共享的稳定领域对象。

**Files:**
- Create: `src/interview_copilot/domain/models.py`
- Create: `src/interview_copilot/domain/events.py`
- Create: `src/interview_copilot/domain/session.py`
- Create: `tests/v2/domain/test_events.py`

**Steps:**

1. 写事件序列化测试，覆盖 session_id、turn_id、generation、segment_seq、created_at。
2. 定义不可变 PCMChunk、AudioSegment、Transcript、Question、AnswerEvent、ProviderStatus。
3. 定义 SessionState，避免 module-level mutable singleton。
4. 实现事件 JSON 序列化，不泄露 bytes 或本地路径。
5. 运行领域测试。

---

## Phase 2：输入与转写迁移

### Task 7：定义 AudioSource 与 Transcriber 协议

**Objective:** 建立跨平台输入和 STT 的稳定接口。

**Files:**
- Create: `src/interview_copilot/audio/interfaces.py`
- Create: `src/interview_copilot/providers/stt.py`
- Create: `tests/v2/audio/test_interfaces.py`

**Steps:**

1. 写 FakeAudioSource/FakeTranscriber 契约测试。
2. 定义 start/read/stop/close 和 transcribe/close 协议。
3. 明确 None 表示暂时无数据，EOF/关闭由独立状态表达。
4. 确认接口不引用 PyAudio、FastAPI 或 httpx 类型。

### Task 8：迁移模拟输入

**Objective:** 把 `SimulatedAudioInput` 迁移为正式 `SimulatedAudioSource`。

**Files:**
- Create: `src/interview_copilot/audio/simulated.py`
- Create: `tests/v2/audio/test_simulated_source.py`
- Legacy reference: `copilot/audio.py:35-59`

**Steps:**

1. 写 start/read/stop/close、重复关闭、队列满和 EOF 契约测试。
2. 实现带 source_id、timestamp、sample_rate、channels 的 PCMChunk 输出。
3. 不提供公开无鉴权上传 API。
4. 旧 SimulatedAudioInput 暂时保留，由 adapter 转接。

### Task 9：抽取 Segmenter

**Objective:** 把声道混合、重采样、校准、VAD 和分段从 AudioEngine 拆出。

**Files:**
- Create: `src/interview_copilot/audio/segmenter.py`
- Create: `tests/v2/audio/test_segmenter.py`
- Legacy reference: `copilot/audio.py:402-570`

**Steps:**

1. 将现有 VAD 参数冻结写入 fixture，不改数值。
2. 为静音、语音、语音后静音、max segment 和 calibration replay 写行为测试。
3. 实现纯 Python Segmenter：PCMChunk → AudioSegment。
4. Segmenter 不创建线程、不发 HTTP、不广播事件。
5. 比较迁移前后算法逻辑，记录任何不可避免的差异。

### Task 10：实现 OpenAI-compatible Transcriber

**Objective:** 把 STT HTTP、鉴权、解析、超时和错误统一放入 provider。

**Files:**
- Modify: `src/interview_copilot/providers/stt.py`
- Create: `tests/v2/providers/test_stt.py`
- Legacy references: `copilot/stt.py`, `copilot/audio.py:571-607`

**Steps:**

1. 为 JSON、plain text、401、429、5xx、timeout 和关闭写失败测试。
2. 实现 connect/read/write/pool 分离 timeout。
3. 明确可重试与不可重试错误。
4. 添加 close()，关闭后拒绝新请求。
5. 不在 provider 内决定 question 合并或 UI 文案。

### Task 11：实现 TranscriptionService 和 legacy adapter

**Objective:** 用 v2 source + segmenter + transcriber 形成完整但不接管默认入口的链路。

**Files:**
- Create: `src/interview_copilot/services/transcription.py`
- Create: `src/interview_copilot/legacy/transcription_adapter.py`
- Create: `tests/v2/services/test_transcription_service.py`

**Steps:**

1. 写 session/generation/seq 传播测试。
2. 写 stop 后迟到结果丢弃测试。
3. 实现有界任务队列和显式取消。
4. adapter 将 v2 Transcript 映射为旧 `question` event。
5. 保持旧 server 默认走旧链路；用配置开关启用 adapter。

---

## Phase 3：会话、回答与持久化

### Task 12：建立 SessionManager

**Objective:** 消除全局 HISTORY、ACTIVE_QA、ENRICH、GEN_SEQ。

**Files:**
- Create: `src/interview_copilot/services/interview.py`
- Create: `tests/v2/services/test_session_manager.py`
- Legacy references: `server.py:63-69,301-386`

**Steps:**

1. 写两个会话并发互不污染测试。
2. 写新题递增 generation、清空只取消本 session 测试。
3. 实现 SessionManager 和不可变任务上下文。
4. 所有异步回调只使用捕获的 session/turn/generation。

### Task 13：重写历史 Repository

**Objective:** 消除 writer 可变 session_id 竞态。

**Files:**
- Create: `src/interview_copilot/persistence/interfaces.py`
- Create: `src/interview_copilot/persistence/sqlite_history.py`
- Create: `tests/v2/persistence/test_sqlite_history.py`
- Legacy reference: `copilot/history.py`

**Steps:**

1. 写“旧 session 排队事件 + 新 session 创建”隔离测试。
2. 每条命令显式携带 session_id/turn_id。
3. 使用临时数据库和可控 flush barrier。
4. 实现 close()/flush()。
5. 编写旧 DB 只读迁移器，但不执行真实迁移。

### Task 14：建立 LLMProvider 与 AnswerService

**Objective:** 将 provider 连接和回答业务编排分离。

**Files:**
- Create: `src/interview_copilot/providers/llm.py`
- Create: `src/interview_copilot/services/answer_generation.py`
- Create: `tests/v2/providers/test_llm.py`
- Create: `tests/v2/services/test_answer_generation.py`

**Steps:**

1. 冻结当前 Prompt 文本，不在结构迁移中优化。
2. 为首 token、流间隔、HTTP 错误、取消和 fallback 写 provider 测试。
3. AnswerService 接受不可变 AnswerRequest。
4. 每个 delta/done/error 都携带 session/turn/generation。
5. 旧 LLMClient 暂时保留，由 adapter 调用 v2 provider。

### Task 15：抽象知识库 Provider

**Objective:** 分离材料读取、检索、摘要和联网搜索。

**Files:**
- Create: `src/interview_copilot/providers/knowledge.py`
- Create: `src/interview_copilot/services/enrichment.py`
- Create: `tests/v2/providers/test_knowledge.py`
- Legacy references: `copilot/knowledge.py`, `copilot/materials.py`, `copilot/divergent.py`

**Steps:**

1. 定义 KnowledgeDocument、RetrievedChunk、Provenance。
2. 将本地论文、材料、Obsidian、Web Search 分成独立 source。
3. Summarizer 独立于材料 ingest。
4. 网络增强默认关闭并显式显示状态。
5. 删除只在 v2 repository 中实现完整磁盘清理；legacy 不改。

---

## Phase 4：v2 API 与生命周期

### Task 16：建立 FastAPI application factory

**Objective:** 用 lifespan 装配和释放所有资源。

**Files:**
- Modify: `src/interview_copilot/app.py`
- Modify: `src/interview_copilot/lifecycle.py`
- Create: `tests/v2/api/test_lifespan.py`

**Steps:**

1. 写 startup/shutdown 资源顺序测试。
2. create_app 只接收 Settings/可选依赖覆盖。
3. lifespan 创建 repository/providers/services。
4. shutdown 取消任务、停止 source、关闭 client、flush DB。
5. import app 不执行 startup。

### Task 17：实现 `/api/v2` 路由

**Objective:** 提供 session-scoped 命令和查询 API。

**Files:**
- Create: `src/interview_copilot/api/schemas.py`
- Create: `src/interview_copilot/api/routes.py`
- Create: `tests/v2/api/test_routes.py`

**Steps:**

1. 实现 session 创建、输入启停、手动问题、深挖、历史查询和 provider status。
2. 所有输入使用 Pydantic schema 和大小限制。
3. 本地文件路径输入默认禁用或限制允许目录。
4. 上传使用唯一临时文件、流式大小限制、finally 清理。

### Task 18：实现可靠 SSE

**Objective:** 统一事件协议并定义背压、断线和重连行为。

**Files:**
- Create: `src/interview_copilot/api/sse.py`
- Create: `tests/v2/api/test_sse.py`

**Steps:**

1. 为 keepalive、核心事件、状态事件合并、队列满和断线写测试。
2. 为事件分配 event_id。
3. 支持客户端携带 Last-Event-ID 补拉有限事件。
4. 核心事件不能静默丢弃；状态电平允许合并。

### Task 19：接入旧 server 的 v2 feature flag

**Objective:** 在不切换默认入口的情况下允许人工选择 v2 后端链路。

**Files:**
- Modify: `server.py`（仅添加装配开关，不继续扩展业务）
- Create: `src/interview_copilot/legacy/server_adapter.py`
- Document: `docs/migration-flags.md`

**Steps:**

1. 增加 `backend_version=legacy|v2` 本地配置项。
2. 默认保持 legacy。
3. v2 模式挂载 `/api/v2`，根页面仍旧版。
4. 提供明确回滚方法。

---

## Phase 5：模块化前端

### Task 20：建立前端目录和静态入口

**Objective:** 拆分单文件页面，但保持现有视觉和文案。

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/styles/tokens.css`
- Create: `frontend/styles/layout.css`
- Create: `frontend/styles/components.css`
- Create: `frontend/js/app.js`
- Keep: `static/index.html`

**Steps:**

1. 复制结构，不直接修改旧页面。
2. 抽取 CSS，保持相同 tokens 和布局。
3. 新页面使用 `/v2` 独立入口。
4. 不引入 npm/Vite。

### Task 21：建立前端 API、事件和 Store

**Objective:** 消除组件内直接 fetch/EventSource 和散落全局变量。

**Files:**
- Create: `frontend/js/api.js`
- Create: `frontend/js/events.js`
- Create: `frontend/js/store.js`
- Create: `tests/frontend/store.test.mjs`

**Steps:**

1. 定义 normalized store。
2. API client 只处理请求和错误映射。
3. Event client 处理连接状态、重连和事件序列。
4. Store reducer 按 session/turn/generation 拒绝过期事件。

### Task 22：拆分 UI 组件

**Objective:** 将系统状态、音频、卡片、知识库和历史拆为独立组件。

**Files:**
- Create: `frontend/js/components/system-status.js`
- Create: `frontend/js/components/audio-controls.js`
- Create: `frontend/js/components/question-card.js`
- Create: `frontend/js/components/answer-card.js`
- Create: `frontend/js/components/knowledge-panel.js`
- Create: `frontend/js/components/history-panel.js`

**Steps:**

1. 组件只接收 state + dispatch callback。
2. 所有动态文本使用 textContent；必要富文本使用受控 renderer。
3. 显示等待、处理中、重试、取消、失败和完成。
4. 诊断区域显示服务/STT/LLM/输入/知识库状态。

### Task 23：提供新旧页面切换

**Objective:** 让用户可在旧 UI 和 v2 UI 间安全切换。

**Files:**
- Modify: `src/interview_copilot/api/routes.py`
- Modify: `server.py`
- Create: `docs/frontend-migration.md`

**Steps:**

1. `/` 仍指向 legacy。
2. `/v2` 指向模块化页面。
3. 页面显示当前 backend/frontend 版本。
4. 提供一键返回 legacy 的链接。

---

## Phase 6：数据迁移、切换与发布

### Task 24：实现用户数据目录

**Objective:** 新版运行数据不写入源码目录。

**Files:**
- Modify: `src/interview_copilot/settings.py`
- Create: `src/interview_copilot/storage_paths.py`
- Create: `tests/v2/test_storage_paths.py`

**Steps:**

1. 评估并决定是否采用 `platformdirs`。
2. Windows 默认 `%LOCALAPPDATA%/InterviewCopilot/`。
3. 数据、缓存、日志、配置分别建目录。
4. 支持显式 override，禁止自动迁移/删除旧数据。

### Task 25：编写只复制的数据迁移工具

**Objective:** 将旧数据复制到用户数据目录，并保留可验证回滚。

**Files:**
- Create: `tools/migrate_legacy_data.py`
- Create: `docs/data-migration.md`

**Steps:**

1. dry-run 默认开启。
2. 复制前列出简历、材料、论文、DB 和 metadata。
3. 复制后按 hash/size 验证。
4. 不删除旧目录。
5. 用户明确确认后另行制定删除计划。

### Task 26：功能等价门禁

**Objective:** 在切换默认入口前证明 v2 覆盖旧版核心功能。

**Files:**
- Create: `docs/feature-parity-checklist.md`

**Checklist:**

- 双输入控制
- 模拟输入
- VAD/STT
- 问题合并
- 快速卡/深挖
- 简历/论文/材料
- 历史保存和导出
- 手动输入
- 错误与诊断
- 停止/重启/清空

未经该清单全部通过，不切换默认入口。

### Task 27：切换默认入口并归档 legacy

**Objective:** v2 通过门禁后成为默认版本，旧版保留回滚入口。

**Files:**
- Move after verification: `server.py`, `copilot/`, `static/` → `legacy/`
- Update: `README.md`, launcher scripts
- Create: `CHANGELOG.md`

**Steps:**

1. 切换前生成文件移动清单。
2. 更新所有引用路径。
3. 默认入口切到 v2；`/legacy` 保留一个发布周期。
4. 更新 README 和 CHANGELOG。
5. 不删除 legacy。

### Task 28：最终开源发布审查

**Objective:** 确认公开仓库没有个人数据、密钥、本机路径或未声明依赖。

**Steps:**

1. 敏感文件和绝对路径扫描。
2. 发布候选目录与 private inventory 对照。
3. 全新环境安装和离线默认测试。
4. Windows 真实设备验收。
5. 前端浏览器视觉和交互验收。
6. 第三方独立代码审查。
7. 用户确认后再创建公开仓库。

---

## 实施顺序摘要

1. Phase 0 开源隔离
2. Phase 1 v2 骨架
3. Phase 2 音频/STT
4. Phase 3 会话/LLM/持久化
5. Phase 4 API/lifecycle
6. Phase 5 模块化前端
7. Phase 6 数据迁移/切换/发布

任何阶段失败时，默认入口仍保持 legacy。禁止一次性移动旧目录或直接删除个人数据。

## 开放决策

1. LICENSE：MIT、Apache-2.0 或其他。
2. 是否引入 `platformdirs`。
3. v2 前端稳定后是否引入 Vite；首阶段不引入。
4. 旧 `/legacy` 保留几个发布周期。
