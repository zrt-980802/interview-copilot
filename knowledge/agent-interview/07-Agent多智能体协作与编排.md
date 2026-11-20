# Agent 多智能体协作与编排

同义词：Multi-Agent、多智能体、Supervisor、监督者、Handoff、交接、Router、路由、Orchestrator、编排、Blackboard、共享黑板。本文是通用技术资料，不证明候选人实现过这些方案。

## 一句话结论

多 Agent 的价值是用明确的角色、上下文和权限边界分离复杂任务，不是让多个模型自由聊天。只有当一个 Agent 的工具集、Context 或决策空间已经过大，且子任务可分离验证时，多 Agent 才可能值得额外的延迟、成本和一致性复杂度。

## 什么时候需要多 Agent

适合场景包括：子任务需要完全不同的工具或权限；不同领域需要独立 Context；子任务可并行且可分别验收；或调度者只需分解和合并，不应获得子 Agent 的高风险工具。

如果只是 Prompt 不同，往往用一个 Agent 加确定性步骤或子函数更简单。不要把每个工具包装成 Agent，也不要用角色数量表示系统智能程度。

### 什么时候应该从单 Agent 拆成多 Agent

先用单 Agent 建立成功率、延迟和成本基线。只有当失败能明确归因于工具、Context 或权限过载，且子任务可独立验收、并行或故障隔离时才拆分。如果只是 Prompt 不同，用子函数或确定性 Workflow 通常更简单。

### 多 Agent 一定比单 Agent 强吗

不一定。多 Agent 会增加路由错误、上下文损失、重复调用、结果冲突和调试难度。先用单 Agent 基线测量失败是否真的来自职责或 Context 过载，再对可分离的子任务拆分，并比较任务成功率、延迟、Token 成本和失败扩散。

## 常见编排模式

**Router** 先分类意图，将任务交给一个专用 Agent。它适合领域边界清晰的场景，但需要未知类别、低置信度和路由错误的回退。

**Supervisor** 保留总目标和全局状态，逐步调度专家 Agent。优点是控制集中，缺点是 Supervisor 可能成为 Context、性能和正确性瓶颈。

**Handoff** 把当前对话的控制权转给另一 Agent。交接应包含结构化任务、已确认事实、待办、权限和返回条件，而不是无限复制全部对话。

**Planner-Workers** 先生成依赖图，再并行运行无依赖的 Worker，最后由 Synthesizer 合并。它适合研究和批处理，但计划、资源上限和合并规则必须可验证。

**Blackboard** 让多个 Worker 围绕受控共享状态协作。共享状态需要 Schema、版本、并发控制和来源，不能只是一段所有 Agent 都能覆盖的文本。

## 交接契约与状态

交接输入至少包含 `task_id`、子目标、已知事实与来源、约束、可用工具、预算、截止时间和期望输出 Schema。返回值区分完成、部分完成、需要澄清和失败，并附证据、未解决项和剩余风险。

权威任务状态由编排层或持久化状态机管理。消息重放时使用幂等任务 ID；过期 Worker 返回要通过世代号或任务版本拒绝，防止旧结果覆盖新计划。

## 冲突、合并与完成判定

合并器不能仅凭文字流畅度选结果。对事实冲突先比较来源权威性、时间和适用范围；对外部状态从权威系统重新读取；无法消解时显式保留分歧或请求人工判断。

系统完成不等于所有 Agent 都说“完成”。编排器根据依赖图、结构化产物和外部 Verifier 判定，并记录部分失败是否允许降级。

## 并行、预算与失败隔离

只有无数据依赖、无写冲突且总预算允许的子任务才并行。全局预算需分配到 Worker，防止每个 Agent 都以为自己拥有全部 Token、时间和工具额度。

子任务超时不应无限级联取消。根据依赖关系选择取消下游、使用部分结果、替换 Worker 或转人工。每个 Worker 有独立超时、重试和断路器，编排器还有总截止时间。

## 常见失败场景

- Router 置信度低仍强制路由，错误领域开始执行。
- Agent 之间传递全部对话，成本增加且事实边界模糊。
- 多个 Worker 同时修改同一资源，最后写入覆盖早期结果。
- 一个 Agent 的错误结论被后续 Agent 当成已验证事实。
- Supervisor 反复调用专家，没有全局步数和费用上限。
- 定义了多个角色，但所有 Agent 拥有相同的高权限工具。

## 生产实现检查表

先有单 Agent 基线；每个角色有单一职责和最小权限；交接使用结构化契约；编排层保存权威状态；全局与子任务都有预算；用任务版本拒绝过期结果；合并保留来源和冲突；完成由外部条件验证；Trace 能串联父子任务；评测与单 Agent 比较质量、成本和长尾延迟。

## 20 秒口述

多 Agent 不是角色越多越好。我会在工具、权限或 Context 确实需要分离，且子任务可独立验收时才拆分。编排器保存权威状态和总预算，Agent 通过结构化契约交接，合并器按证据和外部验证处理冲突。

## 1 至 2 分钟展开

我会先做单 Agent 基线，确认问题是工具过多、Context 污染还是权限需要隔离。如果拆分，我会为每个 Worker 定义目标、输入事实、工具白名单、预算和输出 Schema。编排层管理依赖图、版本和截止时间，无依赖任务才并行。合并时保留来源和分歧，外部 Verifier 决定是否完成。最后和单 Agent 比较成功率、P95 延迟、Token 和调试成本，如果没有明确收益就不保留复杂度。

## 高频追问

### Supervisor 会不会成为单点瓶颈

会。可以让 Supervisor 只处理粗粒度状态和调度，将大文档保存在外部存储；对无依赖任务并行调度；持久化检查点以支持恢复。但不应通过无限增加 Supervisor 层级来解决瓶颈。

### 多 Agent 怎样评测

除最终任务成功外，还检查路由准确率、交接完整性、冲突处理、重复工作、子任务失败隔离、全局预算和长尾延迟，并与单 Agent 及确定性 Workflow 做对照。

## 系统设计练习

设计企业研究 Agent：Planner 将主题拆成公司、市场和风险子任务；Worker 只能访问对应数据源，输出 Claim-Evidence 列表；Verifier 检查时效和引用；Synthesizer 只基于通过验证的证据合并，保留冲突。编排器限制总并发、费用和截止时间，单个数据源失败时显式降级而不是伪造结果。

## 参考资料

- Anthropic, Building Effective Agents: https://www.anthropic.com/research/building-effective-agents
- OpenAI Agents SDK, Agent orchestration: https://openai.github.io/openai-agents-python/multi_agent/
- LangGraph, Multi-agent systems: https://langchain-ai.github.io/langgraph/concepts/multi_agent/
- Microsoft AutoGen documentation: https://microsoft.github.io/autogen/
