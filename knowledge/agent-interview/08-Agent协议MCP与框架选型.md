# Agent 协议 MCP 与框架选型

同义词：MCP、Model Context Protocol、Host、Client、Server、Tools、Resources、Prompts、A2A、Agent-to-Agent、Agent SDK、LangGraph、AutoGen。本文是通用技术资料，不证明候选人使用过某个框架。

## 一句话结论

MCP 解决应用如何用统一协议发现并调用外部工具和上下文，A2A 类协议更关心独立 Agent 间的任务协作。协议不会替你完成授权、幂等、评测或编排；框架选型应从状态、恢复、可观测和运行边界出发，而不是从示例代码长度出发。

## MCP 的角色边界

MCP Host 是面向用户的应用或 Agent 运行时，管理模型、权限和多个连接。Client 是 Host 内对某个 Server 的协议客户端。Server 暴露能力，常见原语包括可执行的 Tools、可读取的 Resources 和可复用的 Prompts。

模型可以建议调用 Tool，但 Host 才负责决定是否将请求发给 Server。Server 返回的名称、描述和内容都不是高优先级指令，也不因为走了 MCP 就可信。

### MCP 是不是 Function Calling

不是同一层。Function Calling 是模型与应用之间表达结构化动作的机制；MCP 定义应用如何与能力提供方建立会话、发现能力和交换消息。Host 可把 MCP Tool 转换为模型的 Function Calling Schema，但中间仍需要策略和执行层。

## 能力发现与版本

能力发现使工具不必全部硬编码在 Host，但动态发现不等于动态授权。Host 应对 Server 身份、允许能力、Schema 变化和风险等级做校验，可以钉住可信版本或在变更时重新审批。

工具名不是稳定语义契约。生产环境要对输入输出 Schema、错误类型、副作用、超时和兼容性做版本化，并在升级前运行合同测试。

## 传输与信任边界

本地子进程传输减少网络暴露，但子进程仍获得当前用户的文件和环境权限；远程传输需要 TLS、服务身份、用户授权、超时和重放防护。不能把“本地”解释为“安全”，也不能把 HTTPS 解释为“已授权”。

Host 应按 Server 隔离凭据和数据。不将一个 Server 的返回自动传给另一个 Server，不把凭据注入模型 Context，不让外部内容自行扩大工具白名单。

## MCP 与 A2A 怎样区分

MCP 主要是 Agent 应用与工具或上下文服务之间的接入契约。A2A 类协议面向具有自身身份、能力和任务生命周期的 Agent，常需要能力声明、任务创建、进度、产物、取消和失败语义。

两者可以组合：一个专用 Agent 通过 A2A 接收任务，内部通过 MCP 使用数据和工具。但系统仍需要统一的用户身份映射、授权传递、Trace 关联和费用责任。

## 框架选型维度

先判断需要的是简单工具循环、持久化状态图、多 Agent 交接，还是分布式任务平台。然后比较：状态是否显式；是否支持检查点、取消和人工审批；工具和模型是否可替换；Trace 能否导出；错误和重试是否可控；是否能在不启动真实模型时测试。

还要看协议逃生口：能否拿到原始消息和状态，数据能否迁移，核心业务是否依赖框架私有对象。社区热度和 Demo 行数可以考虑，但不是生产适用性的替代。

### 要不要自己实现 Agent 框架

不应重写标准的模型客户端、协议解析和 Trace 管道，但业务状态机、授权和成功判定必须由产品自己掌握。对只有几个步骤的流程，直接的应用代码可能比引入完整框架更可审计。

## 常见失败场景

- 把 MCP Server 宣称的 Tool 自动加入高权限白名单。
- 把协议层的 Schema 合法当成业务授权通过。
- Server 升级后修改副作用或返回语义，Host 没有合同测试。
- 为了使用框架特性，把简单 Workflow 拆成不可观测的多 Agent。
- 所有业务状态只存在框架内存对象，进程重启无法恢复。
- 调试时只看最终文本，无法还原协议消息、工具参数和状态变化。

## 生产实现检查表

列出 Host、Client、Server 信任边界；对 Server 做身份验证和能力白名单；远程连接加密且授权；工具 Schema 和副作用版本化；应用层重新校验参数和权限；凭据不进模型 Context；协议请求具有超时、取消和 Trace ID；升级运行合同测试；框架状态可导出和迁移；保留绕过框架的最小调试路径。

## 20 秒口述

MCP 是 Agent 应用接入工具和上下文的标准化协议，不是授权或编排引擎。Host 仍要验证 Server、限制能力、校验参数和保护凭据。框架选型我会重点看状态持久化、取消恢复、Trace、测试性和逃生口，不只看 Demo 是否简短。

## 1 至 2 分钟展开

我会把 MCP 放在接入层理解：Server 提供 Tool、Resource 等能力，Host 管理模型、用户授权和连接，Client 维护与单个 Server 的会话。动态发现只说明“有这个能力”，不代表当前用户可执行。因此 Host 要做 Server 身份、工具白名单、业务授权、超时和审计。框架则根据状态图、检查点、人工审批、可观测和供应商锁定风险来选，核心业务状态和安全边界不交给框架默认值。

## 高频追问

### 用了 MCP 还需要工具适配层吗

需要。协议适配处理连接和消息，业务适配层还要统一错误、权限、超时、幂等、数据脱敏和观测。这样核心控制不依赖某个 Server 的自定义语义。

### 怎样比较 LangGraph、Agents SDK 和 AutoGen

不用“谁最好”回答。用同一个目标场景比较状态模型、持久化与恢复、交接或图表达、工具适配、Trace 导出、测试性、部署边界和锁定成本，用最小原型与故障演练做决定。

## 系统设计练习

为企业内部 Agent 平台设计 MCP 网关：登记中心只接受审核过的 Server 和版本；网关将用户身份映射为资源级授权；工具调用按风险审批；凭据仅在执行时注入；请求具有 Trace ID、截止时间和输出上限；Schema 变化先运行合同测试和 Canary。

## 参考资料

- Model Context Protocol, Specification: https://modelcontextprotocol.io/specification/
- Model Context Protocol, Security best practices: https://modelcontextprotocol.io/specification/draft/basic/security_best_practices
- A2A Protocol specification: https://a2a-protocol.org/latest/specification/
- OpenAI Agents SDK documentation: https://openai.github.io/openai-agents-python/
- LangGraph documentation: https://langchain-ai.github.io/langgraph/
- Microsoft AutoGen documentation: https://microsoft.github.io/autogen/
