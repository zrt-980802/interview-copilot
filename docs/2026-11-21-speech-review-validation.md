# 后台复听纠错验证

2026-11-21。开发分支 `codex/quality-followup`，基线 `b4c27238a95c75270baa0704cd4de169eb41c56a`，本轮实现位于本地工作区。冻结规则 SHA-256 为 `99f7e3b051e71d3b7d20a789fcb3a498f5e825c73a22597de2baace84fbca0a3`，57/58、59 和公开样本的报告一致。

## 策略与配置

- 现有续题归并继续使用已识别的后续内容。新增复听单独读取首次识别对应的连续原始音频，包含相邻窗口中未输出文本的一段。
- `active` 模式提交面试官问题后，跨至少两个窗口，或任一窗口音量低于 −42 dB 时尝试复听。最多读取 24 秒，整段弱于 −45 dB、存在采集缺口或音频已离开缓存时跳过，显示相应状态。旧 `legacy` 路径继续按原流程运行。
- 复听和首答独立运行。同一时刻仅允许一项复听，无排队。先用本地 Turbo，文字有差异时再尝试本地 large-v3。模型使用中文、beam 5、VAD、temperature 0，Turbo 重置转写上下文并限制重复四元组。在测试机上使用 CUDA int8_float16，CPU 路径使用 int8。
- 自动改写要求两个复听稿规范化后相同，分段置信度和音频均满足门槛，并且相对首稿只有一处带足够前后文锚点的限制词插入。至少保留两个字的前锚点和八个字的后锚点。数字、技术名词替换和长条件的漏听保留为存疑。large-v3 不可用时 Turbo 仍可用于核对一致，差异保留原题。
- 解码截止预算 20 秒，包含冷加载。每题最多一次自动纠错并重新生成。修订沿用题号，保留旧题与已生成的旧答案或片段，历史保存模型稿、位置、音频散列和耗时。下一题已经提交、进入新会话、采集切换或同题另有修订时，旧复听结果失效。复听未决、失败或中断的题与答案从后续 LLM 对话上下文排除，即使后续已识别的补充接回原题。
- 默认 `speech.review_enabled=false`。本地试用需在活动流水线启用 `speech.review_enabled` 并重启服务，使用现有本地转写运行时和权重。真实配置、录音和历史未用于运行产品测试。真人样本来自隔离的只读快照。

## 自动对拍

| 样本 | 可复听 / 总数 | 首轮编辑数 | 直接用 Turbo | 直接用 large-v3 | 按冻结规则 | 冻结规则状态 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 57、58 开发录音 | 4 / 13 | 446 | 439 | 452 | 446 | 1 一致，3 存疑 |
| 59 独立验收录音 | 2 / 16 | 120 | 117 | 104 | 120 | 2 存疑 |
| AISHELL 固定分片 | 13 / 100 | 160 | 149 | 152 | 160 | 3 一致，10 存疑 |
| 合成音频原稿与注入漏词 | 6 / 6 | 7 | 22 | 20 | 5 | 1 修正，1 一致，4 存疑 |

真人指标是相对 large-v3 长片段参考稿的字符编辑数，首轮和参考稿可能有覆盖差异。57、58 的参考稿共 370 字，59 共 626 字。两者都有回环和麦克风样本，离线每个样本以窗口集合为单位，包含真实面试中可能不会提交为问题的内容。59 只用于冻结后的验收。

AISHELL 使用第三方单分片固定 100 条提供的参考文本，NFKC、大小写折叠后仅保留字母数字。分母 1,419 字，100 条全部计入。首轮与冻结规则的 CER 均为 11.28%，直接用 Turbo 为 10.50%，直接用 large-v3 为 10.71%。两种直接替换各改善 7 条，分别使 1 条、3 条变差。该结果适用于这个分片的固定子集。

- **找回**。合成首稿漏掉「不能自动付款」中的「不能」。两次复听找回这两个字，编辑距离从 2 降为 0。设为只有一个字的前锚点时，策略保留原题并标记存疑。
- **未解决**。跨数据库合成问句的备用稿分别带有不同转写，冻结规则拒绝修改。59 号回环片段直接用 large-v3 后，相对参考稿的编辑数从 13 降到 2，Turbo 从 13 降到 12，两稿仍有分歧。
- **直接替换的误改**。AISHELL 第 1687 行直接用 large-v3 会丢掉开头的一段重复语句，相对参考文本从 9 个编辑增加到 15。第 1350 行直接用 Turbo 从 4 增至 6。冻结规则在这些用例均保留首稿。百分比合成用例中，两模型把中文数词写成阿拉伯数字，字符编辑指标会惩罚这种格式变化，无法单凭这个距离认定语义出错。

## 耗时与回归

| 测量 | Turbo | large-v3 |
| --- | ---: | ---: |
| 57、58 已加载解码 P50 / P95 | 447 / 606 ms | 946 / 1,171 ms |
| 59 已加载解码 P50 / P95，仅 2 段 | 441 / 545 ms | 953 / 1,360 ms |
| AISHELL 已加载解码 P50 / P95 | 319 / 542 ms | 563 / 926 ms |
| 实际后台进程冷启动与解码，合成漏否定 | 5.89 s | 11.16 s |

RTX 4060 Laptop GPU 上实际后台单次合成复听耗时 17.06 秒。整卡显存从 3,996 MiB 峰至 6,315 MiB，差值 2,319 MiB，包含同机进程。已加载计时来自离线解码，冷启动计时从后台任务开始，均未测量真机首答、音频采集或手机网络延迟。

Python 571 项通过，覆盖首答和复听调度、同题更正、短锚点、证据不足、超时、失败、缺口、停止后无效、追问上下文隔离和新会话。浏览器 75 项通过，用合成会话验证桌面、窄屏手机投影及历史，校验首版答案和依据可回看。真机耳机、真实会议、手机 Wi-Fi、并行 GPU 负载下的首答延迟、CPU 复听时延及进程硬退出后的恢复尚未验证。

建议维持默认关闭。真实录音验收里还没有发生自动更正，冷启动开销明显。合成用例证明现在能改正符合规则的漏否定，但样本规模不足以估计真实误改率。

## 复跑

以下 PowerShell 命令从仓库根目录运行。原始逐样本结果保存于 `data/validation-corpus/runs/speech-review-20261121/`，复跑使用新的输出路径。57、58 与 AISHELL 先于 59。

```powershell
$runtime = '<local-stt-runtime>/Scripts/python.exe'
$turbo = '<local-model-directory>/faster-whisper-large-v3-turbo'
$large = '<local-model-directory>/faster-whisper-large-v3'
$corpus = 'data/validation-corpus'
$common = @('--runtime', $runtime, '--turbo-model', $turbo, '--large-model', $large, '--device', 'auto')
$snapshot = "$corpus/snapshot-2026-11-20"
$output = "$corpus/runs/speech-review-rerun"

.\venv\Scripts\python.exe scripts/evaluate_speech_review.py @common --cohort development --snapshot $snapshot --baseline "$corpus/runs/automatic-asr-dev.json" --output "$output/development.json"
.\venv\Scripts\python.exe scripts/evaluate_speech_review.py @common --cohort public --manifest "$corpus/aishell-fixed-100/manifest.json" --baseline "$corpus/runs/aishell-fixed-100.json" --output "$output/public.json"
.\venv\Scripts\python.exe scripts/evaluate_speech_review.py @common --cohort holdout --snapshot $snapshot --output "$output/holdout.json"
.\venv\Scripts\python.exe -m pytest -q --junitxml "$output/pytest.xml"
```
