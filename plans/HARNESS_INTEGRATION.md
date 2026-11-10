# DeepSeek Harness 工程任务接入与验收

> 状态：接入方案，已识别本机桌面安装，尚未运行工程 Worker。更新日期：2026-11-09。
> 适用范围：用 Harness 承接本项目的开发任务；正式研究运行仍受 [PROJECT_RULES.md](../PROJECT_RULES.md) 第 13 节约束。

## 1. 决定与原因

第一阶段采用 DeepSeek Harness 自带的 `headless` 一次性任务入口，由本项目负责人（下称主审）准备任务、审阅改动和签收验收。它有 `--json` 事件流、持久 Session、`workspace-write` 权限预设及退出状态，适合提交一个边界明确的工程任务。**暂不接入 dsh-superagent-prototype**：其公开版本是围绕一个 GitHub 仓库工作的主动编码代理，部署要求 GitHub 代理账户、仓库、专用 profile、tmux 等；其 Router/Worker 以操作系统用户权限运行。2026-11-09 本机核查到桌面快捷方式指向 `D:\harness\DeepSeek Harness.exe`，内置 `dsh.cmd --version` 返回 `0.2.1-rc.5`。尚未为本项目配置隔离的 headless profile 或工程 Worker。把 Super-agent 服务直接挂在研究数据与正式任务上，会引入比当前需求更大的权限和运维面。

这项决定只授权**开发平面**的受限编码任务。生产研究主链仍只允许 L3b、L4、L5 三处模型调用；Harness 的工程调用与正式研究 run 分开记录，不进入事实、信号和回测结论。将来若把 Harness 用作正式研究调度，必须另行验收固定任务接口，并解决额外模型调用的约束；不能沿用本文件的工程授权。

## 2. 职责与边界

| 责任人 | 负责 | 验收前不能自行决定 |
| --- | --- | --- |
| 主审 | 研究问题、PIT/股票池/撮合/会计口径、数据资格、验收样本、依赖与上游选择、最终合入 | 不以 Agent 的“完成”代替产物和测试证据 |
| Harness 工程 Worker | 限定文件内的接口胶水、报表呈现、文档、重复性重构、已定义样本的实现与局部测试 | 改业务规则、放宽拒答、改预期结果、解封留出、升级研究资格、部署或发布 |
| 确定性研究内核 | L1 读取、计算、下单/账户状态、研究资格与产物哈希 | 接受模型文字作为规则、行情事实或验收依据 |

任务按风险分级：

- **A：主审亲做和签收。** L1 可见性、历史股票池定义、标签时钟、成交及公司行为会计、数据源资质、留出与策略评价标准、三处系统提示词和最终资格门禁。Harness 可提供只读资料整理或反例，不接管结论。
- **B：可交付 Harness 实现，主审逐项审阅。** 已冻结接口的适配、普通报表、日志和诊断、测试夹具、纯机械重构。若改动触及 A 边界，Worker 停止并报告差异。
- **C：可批量委派。** 文档链接整理、格式、只读状态盘点。仍需核对事实与来源。

## 3. 接线图与权限

```text
主审冻结任务卡 + 输入文件哈希 + 验收样本
    -> 隔离的任务副本（只含完成任务所需代码、合成样本）
    -> dsh headless 工程 Worker（一次任务、事件流、有限预算）
    -> 候选改动 + 运行记录 + 测试证据 + 未解决问题
    -> 主审审阅与独立关键样本复核
    -> 接受后由主审写入 D:\lianghua；否则退回或拒收

正式研究请求 -> 受限任务接口 -> L1 固定清单 -> 确定性内核 -> L6 门禁
```

任务副本应由主审创建在独立目录或受控容器。2026-11-09 已在 `D:\lianghua` 初始化本地 Git，并将基线提交 `e8947a1` 推送到用户建立的公开仓库 [`mhx2604696547/lianghua`](https://github.com/mhx2604696547/lianghua)。后续每个工程任务使用独立 worktree；给 Worker 共享 `.git` 写权限前须单独审阅 Git 元数据、钩子和配置的风险。所有接受的工程成果由主审提交并推送；Worker 不直接推送。

Worker 的运行账户只能读任务副本与明确给出的资料，只能写候选目录和任务日志；不给 E 盘旧工程、真实数据湖、留出区、凭据目录、正式归档的读写权限。只读资料使用操作系统只读挂载或单独权限，不能仅在任务卡中标为 `read_only_paths`。建议在独立 OS 账户、容器或 VM 中运行，并在进入真实数据前实测读写隔离。Harness 的工作目录与提示词只是任务上下文；`workspace-write` 只管文件效果，不隔离网络和进程。在 Windows 下官方本地 sandbox 可能报告 `partial`；若隔离探针未证明所需边界，就只给可公开的代码和合成样本，不下发受保护材料。`--approval-policy never` 让无人值守任务在需要扩大权限时失败并留下记录。

不在任务副本放 `.env` 或 API 密钥。官方 headless 会从启动环境、当前目录 `.env` 和 Harness home `.env` 取密钥；启动环境和独立 Harness home 由主审配置，日志不记录密钥。额外插件和网络能力需另做清单，不把上游 README 的安装步骤当本项目授权。

## 4. 工程任务卡：必须填实

每个任务从[任务卡模板](harness_tasks/TEMPLATE.md)复制为 `plans/harness_tasks/<task_id>.md`，字段如下。空的输入或验收条件意味着任务不得交付 Worker。

```yaml
task_id: W2-opening-report-001
class: B
owner: 主审
objective: 用已有 L1 诊断结果生成开盘缺证清单
base_manifest_sha256: <主审填入任务副本全部输入的固定清单哈希>
owned_paths: ["src/ashare_research/reporting.py", "tests/test_reporting.py"]
read_only_paths: ["PROJECT_RULES.md", "README.md", "src/ashare_research/pit.py"]
forbidden_paths: ["E:/security-lab/cnequity-lab", "artifacts/snapshots", "holdout", "credentials"]
acceptance:
  - "缺盘前正向可交易证据时仍输出 unavailable，且显示原 L1 reason_code"
  - "不新增绕过 L1 的数据读取"
checks: ["python -m pytest tests/test_reporting.py"]
budget: {wall_minutes: 30, model_calls: 20, retries: 1}
deliverables: ["候选补丁", "逐文件变化说明", "命令及退出码", "事件日志", "未运行项"]
```

上述只是任务卡格式示例，`reporting.py` 和 `test_reporting.py` **当前未存在**，任务卡也尚未创建。实际派发前，主审要先核对当前源码路径、写独立反例与预期，填真实哈希和资源预算。Worker 不得修改 `AGENTS.md`、`PROJECT_RULES.md`、`ARCHITECTURE.v3.md`、`UPSTREAMS.lock.json`、冻结策略配置、历史快照和验收预期；如确需修改，提出变更请求，由主审处理。

## 5. 接入步骤与可执行门禁

1. **固定版本与模型路由。** 已识别桌面安装附带的 `dsh.cmd` 为 `0.2.1-rc.5`；在隔离环境核对它是否支持 `headless`、所需参数与兼容插件，并记录确切版本、依赖及安装包哈希。为独立 profile 配置一个明确的模型路由，凭据只放启动账户的安全环境，不写任务副本，并做一次无工具调用的连通性试验。Harness 为开发预览，升级前要对照兼容性。当前未做工程 profile、模型配置和隔离试验。
2. **隔离探针。** 用没有敏感数据的副本测试允许写入、禁止写入、E 盘读取、网络、进程访问和日志脱敏；把实际 `full/partial` 结果归档。任一越界可达时，调整 OS/容器边界后重测。隔离前只可做纯公开资料任务。
3. **无研究数据的首个试点。** 用一个 C 类文档任务或 B 类合成样本任务，固定任务卡与基线哈希。在任务目录启动 `dsh --profile headless --permission-mode workspace-write --approval-policy never --json -`，通过标准输入提供任务卡文字，保存标准输出 NDJSON、标准错误和进程退出码。具体命令以所锁版本的 `--help` 复核。
   任务卡中的时间、模型调用和重试上限是监督器的停止条件，不是提示词能强制执行的权限。监督进程按真实时钟限制运行时间，从事件流计数调用，达到上限先要求正常结束并等待退出；若退出失败，记为中断并保留产物，不能自动判成功或无损重试。首个试点须验证这些停止条件和实际资源计数可取得；否则只允许人工看护的单次任务。
4. **检查结束状态。** 进程退出码必须为 0，且事件流末尾 `final.reason.kind` 必须是 `completed`；缺末尾事件、超时、中断或失败都不得按完成处理。随后核对清单、所有文件差分、命令退出码和实际测试日志。Worker 自报“通过”不算测试证据。
5. **主审签收。** 核对所有验收样本，检查是否碰到 A 类边界或越权文件；对时点、数字、会计、资格相关的改动由主审运行独立反例。验收通过才复制候选补丁进入主目录，记录基线哈希、补丁哈希、Harness 版本/模型、任务 ID、审阅人和验收结果。失败记录也保留。
6. **正式调度另关。** 只有固定 submit/query/cancel/artifacts 接口、幂等恢复、L1 单闸门、模型调用约束、权限与停机降级全部验收后，才可把 Harness 接到正式研究任务。开发 Worker 不直接调用 `run-study` 产生可发布市场报告。

重试只能用同一冻结任务卡在同一任务副本继续或新建任务版本；先确认前次进程已退出，保留旧事件和补丁。输入、基线或验收变了就新建任务 ID。取消后不得把可能已经写出的候选文件视作自动回滚。失败形态必须包含 `status`、`原因`、`可安全重试条件`、`停止条件`、`产物路径`。

## 6. 首批可委派与验收效果

先挑一个**不碰真实行情或研究资格**的任务验证交接：例如把当前合成试验报告中的缺证原因做成可展开列表。验收时应看到任务卡、冻结输入清单、候选差分、Harness 事件、程序测试结果、主审复核记录；模拟一条 `unavailable` 数据仍保持缺证显示。这个试点只证明工程协作可控，不提升 `market_backtest_ready`。

当前阻塞是本项目缺少已验证的 headless profile、可用的工程模型路由和通过探针的隔离执行环境；`DEEPSEEK_API_KEY` 启动环境变量未设置。桌面版已安装不等于工程 Worker 已接通。现有研究运行仍为 synthetic / engineering_only。无需为首个任务部署 Super-agent、建立 GitHub 机器人账户或改动 E 盘。

## 7. 核对来源

- [DeepSeek Harness 官方 README](https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md)：开发预览与运行入口。
- [官方 headless 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)：一次性任务、事件流、权限参数及完成判据。
- [官方架构说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)：profile、插件与 SDK 组成。
- [官方 sandbox 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.md)和[安全说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/SAFETY.md)：文件权限边界及隔离局限。
- [Super-agent 原型 README](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/README.md)与[部署说明](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/006-deployment.md)：GitHub 工作模型、权限和部署要求。
