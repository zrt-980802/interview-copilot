# Agent 大模型与 Transformer 基础

同义词：Large Language Model、大语言模型、Transformer、Self-Attention、自注意力、Tokenization、分词、Embedding、向量、Autoregressive、自回归、KV Cache、Context Window。本文是通用原理资料。

## 一句话结论

LLM 通常把文本转为 Token，通过 Transformer 建模当前上下文下下一 Token 的概率分布，再逐步采样生成。它学到的是数据中的统计结构，不自带实时事实库、权限系统或任务完成验证，因此 Agent 需要检索、工具、状态与外部 Verifier。

## Tokenization 与 Token

Tokenizer 把文本切成模型词表中的子词、字节或其他单元，每个 Token 映射为 ID。Token 不等于字、汉字或单词，不同模型的 Tokenizer 计数不同。代码、稀有名称、数字和多语言可能被切成不同数量的 Token。

Tokenization 影响上下文成本、延迟和精确字符处理能力。当任务依赖精确编号、字符串长度或大数算术时，不应只依赖模型隐式处理，使用代码工具验证。

## Embedding 是什么

Token ID 经过可学习的嵌入表转成向量，Transformer 在连续向量空间中计算。另一类 Embedding 模型将字符串或文档映射为语义向量，可用于相似检索、聚类和去重。

向量相似只是模型学到的语义接近，不是事实相等或逻辑蕴含。近义的否定句、旧版与新版政策可能很接近，所以检索还需要关键词、元数据、重排和任务评测。

### Embedding 和模型隐藏层是一回事吗

都是向量表示，但用途和训练目标不同。RAG 中的 Embedding 通常是面向句子或文档相似度的固定长度表示；生成模型内部隐藏状态与具体 Token 位置和当前上下文相关，不应直接当成通用检索向量。

## Self-Attention 的直观解释

对每个位置，模型从当前表示生成 Query，与其他位置的 Key 计算相似度，归一化后对 Value 做加权汇总。这让每个 Token 根据当前任务从上下文获取不同信息。

多头注意力使不同头能学习不同的关系，其后还有前馈网络、残差连接和归一化。自回归生成使用因果遮罩，当前位置不能看未来 Token。

## 位置信息与上下文窗口

注意力本身不表示顺序，模型通过位置编码或旋转位置等机制引入相对或绝对位置。上下文窗口是单次调用能处理的 Token 预算，包含指令、用户输入、工具 Schema、检索文本、历史和输出。

窗口大不代表每个位置都被等质量利用。噪声、冲突和关键信息所在位置都可影响表现。Agent 仍需要检索、摘要和结构化状态，不是把所有历史塞满窗口。

## 自回归训练与生成

预训练常用 Teacher Forcing：给定前文预测真实下一 Token，通过交叉熵优化参数。推理时模型每步把自己生成的 Token 加回上下文，因此早期错误可能影响后续。

下一 Token 目标可以学会丰富语言和世界模式，但它不是事实核查目标。当缺少证据时，流畅的似然续写仍可得到高概率，这是幻觉的一个根本来源。

## Temperature、Top-p 与采样

Temperature 缩放 logits：较低时分布更集中，较高时更平滑。Top-p 从累计概率达到阈值的动态候选集采样。它们调整多样性，不会让错误事实自动变真。

结构化工具选择和可回归任务通常需要较稳定输出；创意生成可使用更高多样性。不同 API 对参数语义和默认值可能不同，必须用目标模型实测。

### Temperature 设为 0 就完全确定吗

不能作这个承诺。后端并行、数值精度、模型版本、提供商更新和相同分数的选择都可导致差异。低温度降低采样随机性，但稳定回归还需要版本固定、结构化校验和允许少量变化的评分方式。

## KV Cache 与推理性能

自回归生成每步都需要与过去位置做注意力。KV Cache 保存已有 Token 的 Key 和 Value，避免每步重算整个前缀。它改善 decode 效率，但随上下文长度和并发占用显存。

推理常分为 prefill 和 decode：prefill 处理整个输入，decode 逐 Token 生成。长 Prompt 增加首 Token 时间和 KV Cache，长输出增加 decode 时间；因此优化上下文和输出长度解决的是不同瓶颈。

## 幻觉与能力边界

幻觉不只是“模型不知道”，还可以来自检索错误、上下文冲突、工具失败被当空结果、指令要求必须回答、以及生成后引用未真正支持 Claim。对应处理是证据检索、工具状态、允许不确定和可验证的输出。

模型在文本中表达“我已执行”不代表外部世界已改变。Agent 控制器从工具返回和权威系统更新状态。

## 常见失败场景

- 把 Token 当成字或单词，用字符数直接估算所有模型成本。
- 把 Embedding 相似当成事实支持，忽略否定、时间和权限。
- 认为 Context Window 足够大就不需要检索与记忆策略。
- 把 Temperature 当成事实正确性开关或完全确定性开关。
- 模型生成了工具结果风格的文本，应用误当成真实执行。
- 只用更大模型处理过时知识，没有提供最新证据。

## 生产实现检查表

使用目标 Tokenizer 计算预算；上下文为指令、工具、证据和输出预留空间；Embedding 检索配合元数据和评测；精确计算用工具；生成参数通过目标任务集选择；不把低温度当完全可复现；延迟区分 prefill 与 decode；外部事实和任务完成由证据与权威状态验证。

## 20 秒口述

LLM 将文本切成 Token，用 Transformer 和自注意力结合上下文，自回归预测并生成下一 Token。它学到的是统计模式，不是实时事实库或权限系统，所以 Agent 要用检索获得证据、用工具改变外部状态，再用 Verifier 判定完成。

## 1 至 2 分钟展开

文本先被 Tokenizer 转为 ID 并映射到向量。在 Transformer 中，每个位置用 Query 与其他位置的 Key 计算权重，汇总 Value，加上位置信息、前馈层、残差和归一化得到上下文表示。预训练主要优化下一 Token 预测，推理时逐步采样。KV Cache 避免每步重算旧前缀，但长 Context 仍增加 prefill 和显存成本。这套机制擅长语言与模式生成，但不直接保证事实、权限和外部任务状态，所以生产 Agent 需要确定性控制层。

## 高频追问

### Attention 的计算复杂度为什么值得关心

标准全注意力在序列维度上需要处理位置对，随长度增加计算与显存成本很快增长。实际推理还受 KV Cache、批处理和硬件限制。这是为什么 Agent 不应无限拼接历史，而要检索、摘要和结构化状态。

### 为什么 LLM 会幻觉

因为生成目标是在上下文下产生高概率续写，不是查询权威事实库。训练数据缺失、矛盾或过时，检索错误，以及 Prompt 强迫回答都会加剧。应用层通过可追溯证据、工具查询、允许弃答和事实验证降低风险。

## 系统设计练习

向非模型工程师解释为什么企业问答需要 RAG：LLM 参数保存压缩的训练模式，无法保证包含最新私有文档；RAG 在请求时检索有权访问的证据注入 Context；生成结果保留引用；评测分别检查召回和 Claim-Evidence，而不假设大模型自动知道。

## 参考资料

- Vaswani et al., Attention Is All You Need: https://arxiv.org/abs/1706.03762
- Hugging Face, LLM Course: https://huggingface.co/learn/llm-course/
- Stanford CS336, Language Modeling from Scratch: https://stanford-cs336.github.io/spring2025/
- Jay Alammar, The Illustrated Transformer: https://jalammar.github.io/illustrated-transformer/
