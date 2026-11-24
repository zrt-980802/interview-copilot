# Windows 首次安装阻塞修复与验证

日期：2026-11-24。基线为 `aecdb95589e4714815a367c090ee3bf6726c9b1f`，修复保留为未提交工作区改动，未提交或推送。验证没有读取原仓库的 `config.json`、`data/`、`.env`、个人密钥、真实历史或录音，也没有启动正式服务。

## 修复

- `AnswerEvent.payload` 从直接的 `MappingProxyType({})` 默认值改为 `default_factory`。Python 3.11 可导入，每个事件获得独立只读映射，`frozen` 和外部映射复制语义不变。
- 将 FastAPI 上传路由实际必需的 `python-multipart>=0.0.20,<1` 加入直接运行依赖。它此前只因旧 venv 恰好安装而未暴露，新的 `.[audio,pdf]` 环境会在路由导入时报缺失。
- README 改为从解释器发现、创建 venv、安装、仅首次复制配置到启动的完整路径，不再假定仓库已有 `venv`。
- 新增 Windows 首次运行指南，区分页面、回答 Provider、实时 STT、设备、录音和本地权重；说明数据流、可能费用与排障边界。
- 工作区准备区新增独立“实时转写”状态，只根据本地配置显示未配置、本机/远程服务和“连接未检查”。状态读取不主动访问 STT 或外网。

## Python 3.11 隔离安装

机器上的 Launcher 输出包含厂商标签：

```text
-V:Astral/CPython3.11.15 <user-profile>\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe
```

`py -3.11` 在该注册方式下不可用；`py -V:Astral/CPython3.11.15` 与 `uv python find 3.11` 均能找到解释器。本次使用 uv/Astral CPython 3.11.15 的绝对路径创建全新 venv：

```powershell
<python-3.11.15> -m venv <temporary-validation-root>\venv
<temporary-validation-root>\venv\Scripts\python.exe -m pip install -e '.[audio,pdf]'
<temporary-validation-root>\venv\Scripts\python.exe -m pip install 'pytest>=8,<10'
```

安装从配置的 Python 包索引及本机缓存解析依赖，不是离线安装；实际下载了缺少的包元数据/文件。没有访问回答模型、STT 服务或模型权重。

关键版本：Python 3.11.15、FastAPI 0.165.0、httpx 0.28.1、python-multipart 0.0.34、NumPy 2.4.6、PyAudioWPatch 0.2.12.9、pypdf 6.21.4、uvicorn 0.54.0。`pip check` 报告 `No broken requirements found`。

## 3.11 实际验证

隔离运行目录只复制当前源码、静态资源和公开 `config.example.json`，再复制为临时 `config.json`；没有复制真实配置、`data/` 或 `.env`。

```powershell
<py311> -c "from interview_copilot.domain.models import AnswerEvent ..."
<py311> -m pytest tests\v2\domain\test_events.py tests\regression\test_http_contract.py -q
<py311> -B server.py --port 18747
```

结果：

- `AnswerEvent` 导入成功；默认 `payload` 为独立 `mappingproxy`。
- Python 3.11 聚焦测试：28 passed，1 条既有 Starlette/httpx 弃用警告。
- 真实服务健康检查 `ready=true`，首页 HTTP 200；没有自动开始麦克风或系统声音采集。
- 示例 STT 显示 `whisper-1 · 本机服务 · 连接未检查`，没有假称服务或模型已就绪。
- 示例 Provider 显示未配置；提交合成问题“TCP 为什么不能用两次握手？”后由内置 `local:fact` 完成，没有外部模型请求或费用。
- 结束合成会话并导出无音频 ZIP 成功。关联状态为 `linked_empty`，清单为 `partial`，没有伪造录音或转写。
- 桌面 1365px 与窄屏 390px 实际页面均显示正确准备状态，无水平溢出或页面脚本错误。

合成导出包：`data/validation-corpus/runs/windows-first-run-20261124/synthetic-first-run-package-py311.zip`

SHA-256：`66BD3F0B9A95BF050A0BDA4F929CD37D25F9C1A2BA7E885BE426477D58151AF9`

## 现有环境回归

当前项目 venv 为 Python 3.12.13。

```powershell
.\venv\Scripts\python.exe -m pytest -q
node --test --test-concurrency=1 tests\frontend\*.test.cjs
```

- Python：595 passed，1 条既有 Starlette/httpx 弃用警告。
- 浏览器：77 passed、0 failed、0 skipped；使用隔离离线预览和本机 Chrome。
- 正式服务未重启；测试端口 18737、18738、18747 已关闭。

## 服务、网络、费用与未验证项

- 回答：只运行内置确定性 `local:fact`，没有配置、读取或回退个人/公司 API key，没有模型费用。
- 实时 STT：公开模板的本机 8760 地址未监听，本次没有发起转写请求。页面仍可启动并明确显示连接未检查。
- 音频：未打开真实麦克风、回环设备或会议软件。没有使用合成音频走实时 STT；只验证无音频会话的如实导出。
- 录音：公开模板默认开启且设置页可关闭；本次隔离会话没有音频片段。已有用户设置优先的行为未改变。
- 离线精转写：设备默认保持 `auto`；未安装 `.[offline-asr]`，未加载或下载模型权重，未验证 CPU/CUDA 推理。
- 安装网络：pip 使用包索引/缓存，可能产生普通下载流量；没有外部 Provider、STT 或模型仓库流量。
- 实测版本仅为修复后的 Python 3.11.15 聚焦/真实启动与 Python 3.12.13 全量回归。没有用 Python 3.13/3.14 或第二台 Windows 机器做完整安装，不据此声称这些环境已验收。
- 真实 Provider、远程 STT 的数据处理与费用、真实耳机/麦克风、会议软件回环、长时间录音和真实会议仍需试用者在自己的环境中单独验收。
