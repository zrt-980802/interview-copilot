# M4-2 历史复盘与来源持久化

日期：2026-11-17

基线：`v2-restructure` / `6be3112`。保留所有前序工作区改动，未提交或推送。

本阶段完成历史页面与完成回答的来源保存。M3 回答质量、M4 全部界面及真实手机/音频仍未整体验收。

## 实现

- `services/history.py` 新增 `answer_documents` 表。快速回答按 turn 保存，深挖按旧 details 表的真实 rowid 保存，包含来源摘录、任务标识、模型与尝试次数。原文和文档同事务写入，移除 4000 字截断，入队后调用方修改对象不影响保存内容。
- 旧库升级前使用 SQLite backup API 生成同目录一致性备份，仅新增表，原有表与记录保留。新库不生成升级备份，正常重复启动不重复备份。已在临时旧库验证备份与重启；没有打开个人数据库执行升级。
- 读取历史时使用同一个 SQLite 读快照取得问题、回答、深挖和文档。依据原文和保存的来源重建渲染，不信任持久化 HTML。文档损坏、来源类型或行号异常时退回原文；旧记录不反推来源。
- `SessionManager.completion_snapshot` 在任务有效时生成完成文档；当前 HTTP 回调将同一任务的来源与原文一起持久化，然后完成任务和广播。重启后不依赖最新的个人材料恢复过去来源。
- 历史 API 增加参数校验、字面量搜索、分页和 `has_more`。查询覆盖标题、题目、回答、深挖正文，空会话保留在库中但从列表过滤。flush 和查询在工作线程执行；存储故障返回 503，避免误显示为空。
- `/history` 提供会话列表、搜索、详情、来源展开、深挖折叠、复制和 Markdown 下载。窄屏使用列表/详情切换，刷新恢复所选会话；请求取消和序号保护防止慢响应覆盖新选择。桌面工具栏新增历史图标入口。
- Markdown 导出直接响应下载，不落服务端临时文件或返回本地路径；保留原回答及来源文本，动态代码围栏可容纳原文反引号。页面与历史 API 设置 `no-store`。手机监听端不开放历史页面、接口或静态资源。
- 合成预览改用临时目录内的真实 HistoryStore，供 UI 和 HTTP 持久化联调；仍是假音频与合成模型传输。

## 验证

- 最终 Python 全量：**282 passed**，保留 1 条既有 Starlette/httpx 弃用警告。较 M4-1 增加 9 项，包括旧库备份、长回答和来源重启恢复、文档防篡改回退、事务回滚、覆盖去陈旧来源、搜索分页、HTTP 完成/重启、导出围栏和存储故障响应。
- Playwright 全套：**32 passed / 0 skipped**，原有桌面及手机 27 项通过，新增历史 5 项。最后修改空会话过滤后重启预览，再跑历史 5 项全部通过。
- 浏览器实际使用真实 HTTP 保存快速答案和深挖、搜索、展开引用、读取剪贴板、下载原文及来源，并刷新恢复。另覆盖旧记录、损坏回退、搜索/更多、旧请求延迟、503/404 和窄屏返回。手机配对前后不能访问历史端点。
- 查看桌面、320px 窄屏和等效 200% 浏览器缩放的 CSS 视口截图；另自动检查 390px、长来源和长代码。页面无横向溢出，代码在自身区域滚动，导出按钮保持可见。等效缩放采用 683×450 CSS 视口，不等同于手动操作浏览器缩放菜单或真实手机。
- `git diff --check` 通过；Git 提示的 CRLF 转换信息不是空白错误。

首轮浏览器测试发现窄屏读取失败时缺少返回入口、返回后刷新又重开旧会话，均已修复并回归。Windows 剪贴板会将换行规范化为 CRLF，断言改为按文本行比较。既有双窗口用例增加显式清空测试会话，消除与新历史用例的前置状态耦合。新增存储故障测试初次在 teardown 时仍保留故障注入，已将注入限定到测试上下文，再跑全量通过。

本阶段没有真实模型调用、录音、真实手机/Wi-Fi 验收或修改防火墙；未读写私有配置和个人历史。取消/失败草稿和所有失败尝试的跨重启保存尚未加入。旧版本已丢失或截断的历史内容无法恢复。

## 交付与后续

- 使用与升级/回退说明：[history-review.md](history-review.md)。配置说明、Answer Document 说明和 README 已同步。
- 截图：`docs/evidence/m4-2-ui/`，包括 `history-persisted-desktop.png`、`history-legacy-320.png`、`history-long-desktop.png`、`history-long-390.png`、`history-long-320.png`、`history-long-zoom-200-equivalent.png`。全部使用合成数据。
- 本机合成预览：`http://127.0.0.1:8540/history`，手机界面 `http://127.0.0.1:8541`。仅绑定回环，另一台手机不能直接访问这些地址。
- 下一阶段优先回到 M3 固定案例，减少未知经历被断言为不存在、技术错误和冗长回答；随后继续材料准备/岗位信息面板。真实音频、实机手机、持续运行与最终验收仍在待办中。

```powershell
.\venv\Scripts\python.exe -m pytest -q
# 合成预览：不使用个人数据、真实音频或外部模型
.\venv\Scripts\python.exe tests\regression\run_preview.py --port 8540 --mobile-port 8541
# 使用安装了 Playwright 的 Node，浏览器路径按本机安装配置
$env:INTERVIEW_PREVIEW_URL='http://127.0.0.1:8540'
$env:INTERVIEW_MOBILE_URL='http://127.0.0.1:8541'
node --test --test-concurrency=1 tests/frontend/history.test.cjs tests/frontend/legacy-events.test.cjs tests/frontend/mobile.test.cjs
```
