# 单场面试数据包实现与验证

日期：2026-11-23。实现位于未提交的 `codex/quality-followup` 工作区，未重启正式服务，未提交或推送。测试仅使用合成问答、事件、转写和 PCM WAV，没有读取真实面试、简历、知识库或个人 Provider 配置，也没有外部模型或网络请求。

## 实现范围

- 历史页为已结束的单场会话提供本地 ZIP。文本、实时事件和当前已有的离线转写自动纳入；录音默认不选，用户可分别选择实际可用的麦克风与系统音轨。
- 新会话在历史库与录音清单两侧持久化同一随机关联标识。只有历史 ID、录音 ID 和关联标识全部匹配才合并录音侧数据。旧会话无关联证据时仅导出历史侧文本与事件，不按相似编号猜测。
- WAV 在导出前核对文件名、文件存在性、头、采样宽度、帧数、采样率和声道。进行中会话拒绝导出；缺失、损坏、转写未完成、录音中断和丢包写入清单。磁盘写入失败删除半成品并返回错误。
- ZIP 只包含白名单字段，不包含回答来源快照、简历或知识库摘录、完整配置、日志、认证令牌、其他会话或本机路径。导出不会重新转写、调用回答模型、上传或修改源历史与录音。
- 两类事件文件使用真正的 JSONL：每个事件仅占一个物理行，文本内的中文、换行和引号由标准 JSON 转义保留。
- WAV 除头部、清单字段外，还会以固定 64K 帧块顺序读取全部 PCM 数据。实际可读帧不足、非完整帧或读取失败的片段会在预览和清单中标为不可用，并且不会写入 ZIP。

## 包内容

- `history/session.json`：当前会话问答、深入回答、修订和候选人当时发言文本。
- `events/history.jsonl`：历史库保存的实时识别与决策事件。
- `events/recording.jsonl`：仅在关联核验后纳入的录音相对时间事件。
- `transcripts/offline.json`：当前已经原子保存的离线转写与人工核对文本；不存在时不伪造。
- `audio/microphone/*.wav`、`audio/system/*.wav`：用户选择且验证通过的音轨。
- `manifest.json`、`README.txt`：选择、实际文件、大小、关联、录音/转写状态、缺口和敏感内容提示。

## 现状核查

- 录音设置默认 `enabled=true`，磁盘上的既有设置会覆盖默认值，用户主动关闭不会因本功能被重新打开。
- 离线转写当前默认设备仍为 `auto`，会优先尝试 CUDA 并在自动模式失败时回退 CPU int8；它不等于已实现“CPU 为新默认”。本次未修改设备默认值，也未覆盖既有选择。
- 既有录音导出仍是录音侧转写 JSON，既有历史导出仍是 Markdown；新 ZIP 是单独入口，未改变两者。

## 自动化结果

- `python -m pytest -q`：594 passed，1 条既有 Starlette/httpx 弃用警告。
- `node --test --test-concurrency=1 tests/frontend/*.test.cjs`：77 passed，使用隔离离线预览和本机 Chrome。
- 最终前端改动后再次执行 `node --test tests/frontend/history.test.cjs`：6 passed、0 skipped，覆盖桌面与 390px 导出流程。
- `python -m pytest tests/v2/services/test_session_package.py -q`：8 passed；实际打开生成的 ZIP，以 `json.loads` 逐个非空行解析两类 JSONL，并验证截断 WAV 的预览和最终清单。
- `python -m pytest tests/regression/test_history_review.py tests/regression/test_recording_api.py -q`：7 passed。
- `python -m pytest tests/regression/test_mobile_surface.py -q`：16 passed；移动端没有历史或数据包导出路由。
- `node --check static/js/history.js` 与 `git diff --check`：通过；隔离预览端口 `18737`、`18738` 已关闭。

覆盖双轨、各单轨、无音频、无录音、转写进行中、缺失/损坏 WAV、关联不可靠、进行中会话、磁盘失败、来源不变、无外网、其他会话和虚拟密钥不混入，以及桌面/390px 页面入口、选择、成功和错误状态。

## 合成验收包

完整包：`data/validation-corpus/runs/session-package-20261123/synthetic-interview-package-jsonl-wav-fixed.zip`

截断检测包：`data/validation-corpus/runs/session-package-20261123/synthetic-interview-package-truncated-wav.zip`

完整包清单状态为 `complete`，包含一条麦克风 WAV、一条系统声音 WAV、问答、两类各两个事件和完整合成离线转写。截断检测包保留麦克风 WAV 头、截去末尾 PCM；预览将其标为不可用，最终清单为 `partial`，ZIP 只包含完整的系统音轨。两个包的 JSONL 都已用标准解析器逐行核对事件数量和含中文、换行、引号的内容。该目录受现有 `data/` 忽略规则保护，不会进入提交。

## 已知限制

- 功能上线前已经存在的会话没有持久化关联标识，因此不会合并旧录音；没有做基于编号或时间的推测迁移。
- 音轨按采集通道区分，不是说话人分离；系统声音和麦克风都可能包含环境中的其他声音。
- 不提供批量导出、上传、自动脱敏、加密、逐文件哈希、精确片段裁剪、导入还原或高级诊断。大录音包的最终保存位置和剩余空间仍由浏览器及操作系统决定。
- WAV 完整性校验为了识别末尾截断必须顺序读取每个候选片段的全部 PCM 数据；读取块固定为最多 64K 帧，不会整段载入内存，但很长录音的预览和导出会产生与录音大小成正比的本地磁盘读取时间。
- 自动化验证了合成 PCM 和离线预览，未使用真实设备、真实会议或真实敏感数据。
