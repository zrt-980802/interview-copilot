# Interview Copilot v2 渐进式架构重整设计

> 状态：已批准
> 日期：2026-09-23
> 范围：前后端代码结构重整；本设计不执行测试、不迁移数据、不删除旧代码。

## 1. 目标

在保持当前 Windows 本地版本可用的前提下，建立独立的 v2 架构，逐步替换旧前后端。首个 v2 版本仍以 Windows 为主要平台，但输入、转写和启动边界必须允许未来增加 macOS/Linux 实现。

成功标准：

1. 旧版在迁移期仍可启动和使用。
2. v2 模块导入不会启动线程、创建会话、读取个人文件或调用网络。
3. 音频输入、分段、STT、LLM、知识库、持久化均有明确接口。
4. 后台任务绑定不可变的 session/turn/generation，不读取易变的全局当前状态。
5. 浏览器前端拆成 ES Modules，不引入 React/Vue/Electron/Tauri。
6. 个人数据、运行数据、缓存和源码有清晰的目录边界。
7. 完成结构迁移后再建立测试门禁，避免用旧耦合结构固化测试。

## 2. 非目标

本轮不做：

- 不重新设计回答 Prompt 或评估模型质量。
- 不立即支持 macOS/Linux 音频采集。
- 不引入云端账号、多租户、远程部署。
- 不在重整前运行真实音频、STT、LLM 或浏览器测试。
- 不删除当前个人资料、数据库或旧版代码。
- 不在第一阶段更换 UI 视觉风格。

## 3. 迁移策略

采用 v2 新目录与 legacy 兼容层并行的“绞杀者模式”：

1. 新功能只进入 v2。
2. 旧接口通过 adapter 调用 v2 service，或继续由 legacy 实现。
3. 每迁移一条完整业务链路，再关闭对应 legacy 入口。
4. 未迁移模块不得直接引用 v2 内部对象。
5. 迁移结束后才把旧实现整体移入 `legacy/` 并切换默认入口。

禁止边迁移边大规模修改 Prompt、VAD 参数或页面视觉，以免无法区分结构回归与行为变化。

## 4. 目标目录

```text
interview-copilot/
├─ src/
│  └─ interview_copilot/
│     ├─ __init__.py
│     ├─ app.py
│     ├─ settings.py
│     ├─ lifecycle.py
│     ├─ domain/
│     │  ├─ models.py
│     │  ├─ events.py
│     │  └─ session.py
│     ├─ audio/
│     │  ├─ interfaces.py
│     │  ├─ wasapi.py
│     │  ├─ simulated.py
│     │  └─ segmenter.py
│     ├─ providers/
│     │  ├─ stt.py
│     │  ├─ llm.py
│     │  └─ knowledge.py
│     ├─ services/
│     │  ├─ transcription.py
│     │  ├─ interview.py
│     │  ├─ answer_generation.py
│     │  └─ enrichment.py
│     ├─ persistence/
│     │  ├─ interfaces.py
│     │  └─ sqlite_history.py
│     └─ api/
│        ├─ schemas.py
│        ├─ routes.py
│        └─ sse.py
├─ frontend/
│  ├─ index.html
│  ├─ styles/
│  │  ├─ tokens.css
│  │  ├─ layout.css
│  │  └─ components.css
│  └─ js/
│     ├─ app.js
│     ├─ api.js
│     ├─ store.js
│     ├─ events.js
│     └─ components/
│        ├─ system-status.js
│        ├─ audio-controls.js
│        ├─ question-card.js
│        ├─ answer-card.js
│        ├─ knowledge-panel.js
│        └─ history-panel.js
├─ legacy/
├─ tests/
├─ docs/
├─ config.example.json
├─ pyproject.toml
├─ README.md
└─ LICENSE
```

迁移期间旧 `server.py`、`copilot/`、`static/` 保持原位；只有默认入口完成切换后才整体归入 `legacy/`。

## 5. 后端设计

### 5.1 依赖方向

依赖只能从外向内：

```text
API / platform adapters
        ↓
Application services
        ↓
Domain models + interfaces
        ↑
Providers / persistence implementations
```

Domain 不得 import FastAPI、httpx、PyAudio、SQLite 或项目路径。

### 5.2 配置层

`settings.py` 负责：

- 从 `config.json`、环境变量和合理默认值加载配置。
- 校验 host、port、provider、路径和超时。
- 产生不可变 Settings 对象。
- 不自动读取 Hermes `.env`，不自动扫描桌面。
- 不在 import 时执行 IO。

本地 `config.json` 被忽略；公开仓库只提交 `config.example.json`。

### 5.3 Domain 模型

核心不可变对象：

- `SessionId`, `TurnId`
- `PCMChunk(source_id, sample_rate, channels, captured_at, data)`
- `AudioSegment(session_id, source_id, generation, seq, pcm)`
- `Transcript(session_id, source_id, generation, seq, text, created_at)`
- `Question(session_id, turn_id, text, source_segments)`
- `AnswerEvent(session_id, turn_id, generation, kind, payload)`
- `ProviderStatus(name, state, message, updated_at)`

所有异步结果必须携带 session、turn 或 generation，禁止通过全局“当前题目”判断归属。

### 5.4 音频边界

`AudioSource` Protocol：

```python
class AudioSource(Protocol):
    source_id: str
    sample_rate: int
    channels: int
    def start(self) -> None: ...
    def read(self, timeout: float) -> PCMChunk | None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
```

实现：

- `WasapiLoopbackSource`：Windows 系统回环。
- `WasapiMicrophoneSource`：Windows 麦克风。
- `SimulatedAudioSource`：开发和未来测试 fixture。
- macOS/Linux 以后只新增 adapter，不修改 Segmenter/Transcriber。

`Segmenter` 只做声道混合、重采样、VAD、校准和分段，不发 HTTP，不广播 SSE。

### 5.5 STT 边界

`Transcriber` Protocol：

```python
class Transcriber(Protocol):
    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript: ...
    def close(self) -> None: ...
```

首个实现为 `OpenAICompatibleTranscriber`；本地 faster-whisper 服务只是该协议的一个 endpoint。重试、超时、鉴权、响应解析和错误映射都集中在 provider 内。

### 5.6 Session 与流水线

`SessionState` 保存单个会话的数据：

- history
- active turn
- generation
- current answer
- enrichment cache
- task registry

`InterviewService` 每次新题创建明确的 `turn_id`，再将不可变上下文交给 AnswerService。清空会话会取消该 session 的任务，不影响其他 session。

### 5.7 LLM 与知识库

- `LLMProvider` 负责流式补全、超时、首 token、取消、关闭。
- `KnowledgeSource` 只返回带 provenance 的材料。
- `Retriever` 做检索，不直接调用 LLM。
- `Summarizer` 是独立可选 provider，避免材料模块私下消耗额度。
- 联网搜索默认 opt-in，并在 UI 显示状态。

### 5.8 持久化

Repository 命令全部携带显式 session_id/turn_id，不允许 writer 保存可变的“当前 session”。

推荐初始接口：

- `create_session()`
- `end_session(session_id)`
- `create_turn(session_id, question, self_context)`
- `save_quick_answer(turn_id, content, latency_ms)`
- `save_detail(turn_id, direction, content)`
- `append_event(session_id, event)`

SQLite 文件位于用户数据目录，而不是源码目录。

### 5.9 生命周期

FastAPI lifespan 是唯一装配入口：

- startup：加载 Settings、创建 repositories/providers/services。
- shutdown：停止 source、取消任务、关闭线程池和 HTTP client、flush DB。
- import `app.py` 不得创建历史会话或启动后台线程。

## 6. API 与事件协议

REST 只做命令和查询；SSE 只传领域事件。

建议 v2 前缀：`/api/v2`。

主要接口：

- `POST /sessions`
- `POST /sessions/{id}/start-input`
- `POST /sessions/{id}/stop-input`
- `POST /sessions/{id}/questions`
- `POST /sessions/{id}/turns/{turn_id}/detail`
- `GET /sessions/{id}`
- `GET /sessions/{id}/events`
- `GET /health`
- `GET /providers/status`

事件统一包含：

```json
{
  "event_id": "...",
  "session_id": "...",
  "turn_id": "...",
  "generation": 3,
  "type": "answer.delta",
  "created_at": "...",
  "payload": {}
}
```

SSE 队列满时不得静默丢弃核心 question/answer 事件。状态电平类事件可合并或丢弃；核心事件必须断开并要求客户端重连/补拉。

## 7. 前端设计

### 7.1 技术选择

使用原生 HTML/CSS/ES Modules，不引入构建工具作为第一阶段硬依赖。模块边界稳定后，若确有需要再引入 Vite。

### 7.2 状态管理

`store.js` 保存规范化状态：

- activeSession
- providerStatus
- inputStatus
- currentTurn
- turn list
- knowledgeStatus
- connectionStatus

组件不得直接发 fetch 或持有 EventSource；统一通过 `api.js` 和 `events.js`。

### 7.3 页面区域

1. 系统状态栏：服务、输入、STT、LLM、知识库。
2. 实时区：问题、快速卡、深挖卡、自述转写。
3. 输入区：回环、麦克风、模拟/手动输入。
4. 管理侧栏：材料、历史、设置和诊断。

所有长操作显示阶段：等待、连接、处理中、重试、失败、已取消、完成。不能继续用“无变化”表达错误。

### 7.4 前端兼容迁移

- v2 API 先提供兼容字段给旧页面。
- 新页面通过独立 `/v2` 路径访问。
- 在新页面达到功能等价前，根路径仍指向旧页面。
- 切换默认入口后保留 `/legacy` 一段时间。

## 8. 数据和开源隔离

公开仓库不得包含：

- `config.json`
- `.env`
- `data/history.db*`
- 简历和来源元数据
- 论文/材料全文与缓存
- `venv/`, `__pycache__/`
- 诊断输出、个人路径和历史交付材料

首次运行创建用户数据目录；Windows 使用 `%LOCALAPPDATA%/InterviewCopilot/`。未来平台通过统一 `platformdirs` 解析。

原始个人数据只做迁移副本，不在未经确认时删除。

## 9. 迁移阶段与门禁

### Phase 0：开源隔离

- 配置模板、ignore、依赖与许可证。
- 发布候选目录与个人数据完全分离。
- 不删除旧数据。

### Phase 1：v2 骨架

- 建立 src layout、Settings、domain models、lifecycle。
- 导入 v2 不产生副作用。
- legacy 继续运行。

### Phase 2：输入与转写

- 抽取 AudioSource、Segmenter、Transcriber。
- 旧 server 通过 adapter 使用 v2 transcription service。

### Phase 3：会话、回答和持久化

- 引入 SessionState、AnswerService、Repository。
- 消除全局会话状态和 history writer 的 session 竞态。

### Phase 4：v2 API

- 建立 `/api/v2` 和统一 SSE 事件。
- 提供 legacy compatibility adapter。

### Phase 5：模块化前端

- 拆 CSS/JS/组件。
- 新页面接 v2 API，旧页面保持可用。

### Phase 6：切换与归档

- 完成功能等价和验证后切换默认入口。
- 旧代码整体移入 `legacy/`。
- 最终开源审查和发布。

每个阶段必须满足：旧版仍可运行、没有个人数据进入新目录、没有新增 import 副作用、迁移范围有回滚点。

## 10. 主要风险与缓解

- **新旧两套长期并存**：每阶段定义退役条件，不接受无限期 adapter。
- **行为漂移**：结构迁移期间冻结 Prompt、VAD 参数和 UI 文案。
- **数据迁移损坏**：复制并校验后再切换路径，不删除原始数据库。
- **隐藏全局状态继续渗透**：v2 禁止 import legacy 的 mutable singleton。
- **过度设计**：首版只定义当前确实需要的协议，不引入消息队列、插件框架或前端框架。
- **跨平台范围失控**：只抽象边界，第一版只实现 Windows adapter。

## 11. 设计自检

- 无占位 TODO。
- 平台范围明确：Windows-first，跨平台只预留接口。
- 前端选择明确：浏览器 + 原生 ES Modules。
- 迁移策略明确：渐进并行，不一次性重写。
- 数据删除边界明确：不删除原始个人数据。
- 测试时机明确：完成结构重整后建立测试；本设计阶段不运行测试。
- 未决定项：LICENSE 类型仍需用户选择；v2 是否引入 `platformdirs` 在实施计划中评估。
