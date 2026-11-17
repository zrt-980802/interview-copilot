# Answer Document v1

当前实现：`services/answer_content.py`、`static/js/answer-content.js`。

## 传输与归属

原有 `answer_delta.delta` 和 `answer_done.full` 保持字符串格式，数据库仍保存原回答字符串。新增内容如下：

- `answer_context.sources`：模型调用前，由最终上下文生成的来源列表；只包含本次选择的材料与片段。
- `answer_done.document`：服务端解析后的完整答案文档。
- `session_snapshot.turns[].answers[].document`：同一结构；浏览器刷新后使用它恢复来源与答案。

这些事件沿用 `session_id / turn_id / generation / task_id / level / direction`。过期任务不能登记来源，来源属于具体任务，不能从当前全局题目或最新笔记反查。备用模型使用相同请求上下文，清空文本尝试时保留该任务的输入来源。

来源在 SessionManager 中供实时显示恢复。M4-2 将完成回答的文档、来源摘录和任务/模型信息与原文一起写入历史数据库，服务重启后可从 `/history` 复盘。旧记录不反推来源；细节见 [历史与升级说明](history-review.md)。M4-1 已接入独立手机监听与配对授权，手机只显示当前会话的回答，不开放历史库和背景来源面板。

## 文档字段

```json
{
  "version": 1,
  "level": "quick",
  "complete": true,
  "blocks": [
    {"kind": "conclusion", "title": "", "text": "回答建议", "html": "<p>回答建议</p>", "citations": []},
    {"kind": "material", "title": "context:resume", "text": "接口开发。", "html": "<p>接口开发。</p>", "citations": [],
     "support": {"status": "quote_matched", "source_indices": [0]}},
    {"kind": "unknown", "title": "上线情况", "text": "当前材料未说明", "html": "<p>当前材料未说明</p>", "citations": []}
  ],
  "directions": [{"name": "验证方法", "description": "如何建立基线？"}],
  "sources": [{"id": "context:resume", "source": "context:resume", "kind": "candidate_material", "heading": "", "excerpt": "接口开发。", "partial": false}],
  "findings": []
}
```

`blocks.kind` 支持 `conclusion / point / material / unknown / hypothesis / plan / explanation / text`。深挖为 Markdown `explanation`；旧格式或未识别行保留为正文，不静默丢弃。未完成的回答 `complete=false`，不把半截引用当作完成后的检查失败。

## 行式协议

```text
【开口】结论
【要点】标题 ::: 机制与边界
【材料】context:resume ::: 逐字摘录的一小段原文
【待确认】缺失项 ::: 当前资料未说明的内容
【方案】方案名 ::: 明确标为假设的做法
【验证】指标或方法 ::: 待实施的验证计划
【方向】方向名 ::: 进一步展开的问题
```

一般技术题继续使用开口与要点；经历题按需选择材料、待确认、方案和验证，不要求把所有种类列一遍。材料行标题是来源标识，其余标题为展示文本。只按第一个 `:::` 分隔，正文中的后续分隔符保留。代码围栏里的行式标签是代码正文。

流式草稿由浏览器按同一标签集轻量展示，使用文本节点；完整文档由服务端提供。不等待完整 JSON 才显示首段内容。旧客户端仍可消费原始字符串，新客户端缺少文档时回退为草稿解析。

## 来源与检查的含义

`context:resume` 为本次实际发送的简历节选，`context:candidate_speech` 为本人发言，另有 `context:papers / context:jd / context:enrichment`。各自保留 `kind`，不能把岗位要求、参考笔记或 AI 建议视为候选人实施事实。历史 AI 建议不登记为材料来源。

检索来源保留 `id / source / kind / heading / lines / excerpt` 等字段。行号针对实际发送片段；PDF 行号仍是提取文本行号。简历等经过相关性节选的字符串不伪造连续行号，使用 `partial` 标记。

- `quote_matched` 仅表示材料行文字在指定来源节选中逐字出现，匹配前会归一化空白。不会证明原始材料真实、作者身份正确或该句能支持另一条结论。
- 材料原文无法匹配时，状态为 `unverified`，保留模型文字并提示待核实，不自动升级成个人事实。
- 引用采用 `〔source·L3-5〕`。检索来源需给行号，范围必须被本次发送的连续片段覆盖；有缺口则拒绝。无行号的上下文来源可用 `〔context:resume〕`。
- `source_indices` 是本篇文档 `sources` 数组的下标，不是跨会话身份或文件读取接口。引用点击只展开已发送的片段，不读取任意本地路径。
- 格式错误、未知来源、范围越界、未找到材料摘录等进入 `findings`。代码块或行内代码中的引用符号只作为字面代码。

这些检查不验证技术结论的蕴含关系，也不验证开口或其他段落中的所有个人陈述；`findings=[]` 不能用作语义通过标记。

## 渲染

Markdown 使用 `markdown-it-py` 的 CommonMark 解析器，禁用原始 HTML 和图片；代码、列表和普通链接正常显示。普通链接另开页面并带 `noopener noreferrer`。来源摘录使用文本节点，不执行材料中的 HTML。浏览器不把流式模型文字直接作为 HTML 插入。

展示标签使用“回答建议”“材料原文”“参考摘录”“待确认”“假设方案”“验证计划”。原文匹配是摘录检查结果，不展示为事实认证。引用、来源面板和异常格式均保留在对应题目中。
