# A 股历史状态研究系统：底层架构设计 v3

> 状态：待评审的设计方案，不代表已实现。基线日期：2026-11-06。  
> 依据：[本机实测文档 v1](<E:/security-lab/cnequity-lab/ARCHITECTURE.v1.md>)、[现有修订 v2](<E:/security-lab/cnequity-lab/ARCHITECTURE.md>) 与 [asof.py 原型](<E:/security-lab/cnequity-lab/asof.py>)。外部仓库只提供候选机制，其 README 不能替代本机验收。

> 2026-11-07 范围更新：最终目标已确认是沪深主板、排 ST/新股、盘后信号、次日开盘模拟、持有 1–5 日的短线交易研究，优先交付回测与研究报告。本文件保留为历史状态/证据子系统；全项目以[短线研究任务计划](plans/A_SHARE_SHORT_TERM_RESEARCH.md)为准。下文将回测与 L6 延后的决定仅适用于状态问答路径。新计划包含本次实表核查，更新了证券主表、股本历史及东财部分回填的状态。

## 0. 架构决定

**选单机模块化单体、批量采集、不可变 Parquet 数据湖、DuckDB 只读分析、SQLite 保存运行与结论。** CNEquity 继续承担现有采集和规范数据集；所有历史读取经 L1 时点闸门。八层保留为职责边界，当前产品路径只启用有数据支撑的部分。三处 LLM 调用可关闭；关闭时仍须产出可校验的状态或明确拒答。

如果只能先改一处，改 **L1 的可见性与版本契约**。`announce_date <= as_of` 已通过本机日期过滤测试，却不能证明当前数据库中的值就是该历史时点的版本。发布时间全缺、修正前公告未留存时，严格历史答案必须降级或拒答；上层解释、对抗和门禁不能弥补这一缺口。

本文件对应状态解释子系统，不输出荐股、目标价或实际仓位建议，不接实盘，也不引入 LangGraph、Vibe-Trading 或 tick-stock-panel 的完整运行时。完整短线研究项目按上方新计划建设回测主线，包含用于核算的虚拟组合；本文件的状态卡完成度不能替代研究系统完成度。

## 1. 系统承诺与用户看到的结果

唯一核心问题：**在指定历史截止时刻，只使用能证明当时可见的具体数据版本，这只股票处于什么状态？**

输入至少包含 `symbol`、`cutoff_at`、`cutoff_kind`；运行时由系统固定 `lake_cut_id`、代码版本和提示词版本。`cutoff_at` 使用带时区的时间，A 股数据按 `Asia/Shanghai` 解释。`cutoff_kind` 区分 `market_close` 与 `calendar_day_end`，不接受含义不明的裸日期。

输出是一张状态卡：

- 对可判断的维度给出状态、限定语和事实引用；每个数值来自唯一 `fact_id`，展示时可展开源、字段、披露日、源版本和计算链。
- 对不可判断的维度说“这项不可得”，给出机器可读原因；部分维度可得时，整体状态只能是 `limited`。
- 附 `run_id` 和输入清单哈希。补数或改代码后产生新运行；旧结果不覆写，可比较差异。
- 条件情景只引用程序定义的观察条件，不预测收益或给出买卖动作。

**假设：**当前主要使用者是作者本人，进行单股历史回看。若要把结论交付给他人，需增加人工审阅、发布版本冻结与解释展示；数据和时点底座无需改向。

## 2. 设计必须承认的本机水位

| 已有数据或性质 | 已证实的范围 | 不能据此推断的能力 |
| --- | --- | --- |
| `daily_bars` | demo：5 只股票、每只约 30 个交易日；`pre_close` 全缺 | 六十日量能分位、三年 PE 分位、全市场横截面 |
| `financial_statement_items` | 1,620,056 行；`announce_date` 呈真实披露日分布 | 发布时刻、修正前版本、所有科目跨期齐全、单位已统一 |
| `share_structure` | 茅台样本有历史变更点 | 全市场、全时点股本与其公开可见时刻均已验证 |
| `adj_factors` | 本地可离线派生 | 任意股票与历史窗口都有完整相邻因子 |
| `trading_calendar` | 有自然日及 `is_trading` 标志 | 全部行都是交易日；未来预测节假日必然正确 |
| 抓取运行 | 部分成功的数据仍入库，运行标记失败 | 有行就等于该数据集覆盖完整 |

本文件原基线中，57 个数据集仅 6 个有数据。“1.5 维”表示量能与估值存在部分计算路径，不表示任意股票、任意日期都能给出这两维的完整结论。预期、行业政策、事件维度为空。原基线的东财失败记录保留；2026-11-07 已发现部分股本回填，具体水位见新计划，不能将单个源成功推广到全部接口。外部项目声称可取数仍须本机验收。

## 3. 运行形态与依赖方向

```text
候选数据源 ──> CNEquity / L0 契约适配 ──> 不可变 Parquet 对象 + lake manifest
                                              │
                                              ▼
                                  L1 PitReader：唯一历史读取入口
                                              │ 带可见性状态的证据
                                              ▼
                                  L2 确定性事实与特征
                                              │
                          ┌───────────────────┴───────────────────┐
                          ▼                                       ▼
                状态问答产品线（现在）                     策略研究线（延后）
             L3b → L4 → L5 → 状态卡                 L3 DSL → 回测 → L6
                          │                                       │
                          └──────── L7 运行重放与回归 ────────────┘
                                      facts / claims / runs
```

接口规则：上层只能取得 `PitReader` 返回的带类型证据，不能取得 Parquet 路径、DuckDB 连接或自行拼接时间过滤。当前 `asof.py` 的 `connect()` 会返回裸连接，并用 `*.parquet` 通配符建视图；实现 L1 时要改为读取 manifest 固定的**精确文件列表**。Python 私有属性不是物理隔离：代码扫描和回归测试负责阻断误用；若未来开放第三方插件，再把 PIT 读取移入具有独立文件权限的进程，接口保持不变。

## 4. L0：来源、版本与数据湖切面

### 4.1 数据落盘

每次抓取保存来源、参数、开始与结束时间、覆盖范围、`coverage_status`、失败原因。原始对象按内容哈希不可变存放；新抓取发现同一业务键不同内容时增加版本，不原地改写旧对象。`lake_cut_id` 对应一份 manifest，列出本次可读文件的路径与哈希、数据集覆盖、源契约版本和单位映射版本。

不同数据集来自不同抓取批次，并不必然构成矛盾；**一次回答必须固定同一个 manifest**，且每个所需数据集在其所需时间范围内有完整覆盖。部分成功的抓取可以保存，但缺口进入 `unavailable(partial_coverage)`，不得把股票池静默缩小。

源能力矩阵至少包含：`dataset`、`source`、字段与单位、历史覆盖、公开时间精度、是否留存修正历史、实测可用性、允许用途、故障状态。主源失败不静默换源。新源可以做并行校验；切换主源必须产生新的源契约版本和运行记录。

### 4.2 外部项目借用范围

| 项目 | 借用到本系统 | 本机准入条件 |
| --- | --- | --- |
| [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | 研究的取证、执行、验证、回访流程；版本化研究记录与运行卡 | 只借记录与展示模式；其自动跨源回退不进入 PIT 路径 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 对历史发布时间或原始版本不能确定的数据拒答；运行的日期网格评估思想 | 美股的 `as filed` 数据能力不外推至 A 股；不接 Trader、组合经理或辩论运行时 |
| [a-stock-data](https://github.com/simonlin1212/a-stock-data) | 公告、ST、行业变迁、一致预期等端点的候选清单及源故障信息 | 每个端点分别通过本机可达性、单位、日期、覆盖、版本测试；不接整套自动降级 |
| [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) | 核心与扩展分界、能力矩阵、字段口径失败即拒、可展开的工具足迹 | 所有扩展数据仍入 L0、过 L1；不复制绕过路由的直连路径或未经版本校验的逐字段多源合并 |

优先试点 `trading_status`、官方公告索引与行业变迁；一致预期随后评估。端点存在只说明有取数候选，不能直接把对应维度标为可算。东财相关端点在本机须先验证，不能认为换一个 Python 封装就消除了同一网络或接口故障。

## 5. L1：唯一 PIT 闸门

### 5.1 输入与返回契约

```python
RunSpec = {
    "subject": str,
    "cutoff_at": "带 Asia/Shanghai 时区的时间",
    "cutoff_kind": "market_close | calendar_day_end",
    "lake_cut_id": str,
    "visibility_mode": "verified_visible | reconstructed",
    "code_version": str,
    "prompt_set_version": "字符串或 null",
    "model_id": "字符串或 null",
}

PitResult = VerifiedValue | ReconstructedValue | Unavailable
```

`verified_visible` 要求：所用具体源版本有截止时刻的公开版本证据或同期完整归档快照；时间规则满足；依赖窗口覆盖完整。`reconstructed` 表示披露日可用于事后筛选，但具体历史版本无法证明，仅可供明确标注的诊断。`unavailable` 表示所需时间、版本、单位、依赖或覆盖不成立。产品的严格历史答案只使用 `VerifiedValue`。

`observed_at` 证明系统何时观察到数据，不是源对公众的发布时间。它不能被填进空的 `available_at`，也不能证明晚于截止时刻抓取的值当时已存在。内容哈希 `revision_id` 标识已捕获的版本，不证明没有失落的早期版本。

### 5.2 日精度保守规则

| 数据 | `market_close(d)` | `calendar_day_end(d)` |
| --- | --- | --- |
| 已知精确发布时间 | 比较实际发布时间与截止时刻 | 比较实际发布时间与截止时刻 |
| 只有 `announce_date` 的财报 | 同日公告不纳入；仅 `announce_date < d` | 可以纳入日期不晚于 `d` 的公告，但仍须证明具体版本 |
| 日线 `trade_date=d` | 收盘完成后可纳入 | 可纳入 |
| 股本、因子 | 生效日与公开可见证据分别检查 | 同左 |

交易窗口仅使用 `trading_calendar.is_trading=true` 的日期。停牌日可能是零值行，也可能缺行；在本机取到长期停牌样本之前，不把任一表示写成已证实事实。未确认停牌状态时，可交易性判断为 `unavailable`。

旧湖没有修正前版本时，L1 不得补造过去。自归档启动后持续保存新版本；历史缺口只有通过同期可信归档或源的完整修正历史才能补证。

## 6. L2：事实、特征与缺失传播

每个指标注册四项：输入数据、最小有效样本、时间/单位规则、失败原因。先判能力，再计算。`NULL` 不是一种万能结果；至少区分 `no_dataset`、`insufficient_history`、`missing_dependency`、`unverified_unit`、`unverified_vintage`、`ambiguous_cutoff`、`partial_coverage`、`not_applicable`。

| 指标 | 最小依赖 | 当前 demo 的可用范围 |
| --- | --- | --- |
| 当日收盘价 | 截止前已完成的日线及其版本 | 五只样本的已覆盖交易日，仍依赖版本证明 |
| 量比 `vol_ratio_20` | 当根加此前二十根有效成交日；均值不含当根 | 仅部分靠后的样本日可计算 |
| 近六十根量能分位 | 明确定义为含当根的六十根有效成交日 | 三十根样本不足，必须 `insufficient_history` |
| `PE_TTM` | 对应报告期的累计净利、上年全年及上年同期累计、同一时点股本和收盘价；单位已核实 | 逐票逐期判定，不能按全市场可用宣称 |
| 三年 PE 分位 | 足量且合格的历史 PE 序列；亏损或零利润期不参加分位 | 当前行情窗口不足 |
| 除权日 `pre_close` | 源值，或昨收与相邻有效复权因子 | 因子缺失时不以裸昨收代替价格带宽依据 |

`income` 某期没有 `revenue` 行时，映射表只能识别同义科目，不能制造不存在的观测；找不到经验证等价字段就返回 `missing_dependency`。`pe_ttm <= 0` 不标为“极低估值”，而是该 PE 分位不适用。

当前 [v2 的量能 SQL](<E:/security-lab/cnequity-lab/ARCHITECTURE.md:234>) 先限制 `rn <= 60`，却用 `rn BETWEEN 2 AND 61` 算前六十根均值，实际最多取到五十九根。正式实现要把此类窗口边界放进 golden set。

## 7. L3—L7：只激活有用途的职责

| 层 | 当前决定 | 启用条件与失败形态 |
| --- | --- | --- |
| L3 策略 | 单股状态规则由代码函数表达；L3b 仅解释单维证据 | 三层 DSL 的横截面部分等全市场合格行情，组合部分等明确研究需求；缺指标进入 `unknown` 桶 |
| L4 对抗 | 代码先查 ID、时间、单位、公式和禁用数字；模型找证据支撑缺口 | 模型无发现不等于通过；有效攻击的并集由代码聚合，未解决则 `review_required` |
| L5 决策 | 改名为状态合成；可先用模板，模型仅组织已放行 claim | 不给买卖、仓位、止损价或目标价；维度缺失则整体 `limited` 或 `unavailable` |
| L6 门禁 | 当前产品路径不调用 | 仅在回测器、订单与持仓状态存在后，审计 T+1、涨跌停、停牌和参与率等；否则指标不可得 |
| L7 复盘 | 现在做运行重放、版本差分、回归样本 | 收益归因、PBO、DSR 等延后；`trials` 只记录真实策略试验，普通问答不计为 trial |

DSL 条件将来采用三值逻辑：`all` 有确定假则为假，否则有未知则为未知；`any` 有确定真则为真，否则有未知则为未知。每层漏斗分别记录 `true / false / unknown`，未知不参与排名，也不算淘汰。

模型自报的 `high / medium / low` 或概率不进入产品结果。当前没有预先定义的“命中”标签与期限，不能称其为校准置信度。可由代码显示证据覆盖等级；未来若研究预测，先冻结标签、期限和独立评估集。

`phantom_alpha_share` 当前不可计算。未来须从相同初始状态**重跑**可成交策略，而非删除不可成交交易后直接相减；以同一基准和成本口径比较原始与审计后超额收益。原始超额收益非正或接近零时，该比率返回 `unavailable`，并单独报告收益百分点差。

## 8. 状态存储与数字引用

Parquet 保留大体量源数据与不可变版本。SQLite 只存被一次答案实际引用的派生事实、claim 与运行元数据，不复制整个财务湖。三张核心表的最小契约如下：

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  cutoff_at TEXT NOT NULL,
  cutoff_kind TEXT NOT NULL,
  lake_cut_id TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  code_version TEXT NOT NULL,
  env_lock_sha256 TEXT NOT NULL,
  prompt_set_version TEXT,
  model_id TEXT,
  visibility_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  fact_ids_json TEXT NOT NULL,
  output_artifact_path TEXT,
  output_sha256 TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE facts (
  fact_id TEXT PRIMARY KEY,
  logical_key TEXT NOT NULL,
  subject TEXT NOT NULL,
  metric TEXT NOT NULL,
  as_of_date TEXT NOT NULL,
  value_json TEXT NOT NULL,
  unit TEXT,
  source_version_ids_json TEXT NOT NULL,
  input_fact_ids_json TEXT NOT NULL,
  formula_version TEXT,
  visibility_status TEXT NOT NULL
);

CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  stage TEXT NOT NULL,
  dimension TEXT,
  text_template TEXT NOT NULL,
  fact_ids_json TEXT NOT NULL,
  status TEXT NOT NULL,
  refusal_reason TEXT,
  prompt_version TEXT
);
```

`logical_key` 标识“哪一项事实”，`fact_id` 唯一标识其**具体不可变版本**：

```text
logical_key = H(dataset | subject | metric | as_of_date | source_field)
fact_id     = H(logical_key | 规范化值 | 单位 | 源版本 ID 集合 |
                输入 fact_id 集合 | 公式版本)
```

这样修订后旧值仍有旧 ID，新值有新 ID；`claims.fact_ids_json` 不会像复合键方案那样只指向一个含糊的业务 ID。JSON 数组的成员关系和引用存在性由程序验证；模型不能自行分配事实 ID。若报告必须展示规则阈值，程序也将该阈值物化为带规则版本的事实，再交给模型引用。

最终渲染结果另存为不可变 JSON 运行产物，`runs` 保存其路径与哈希。**查看旧运行**直接读取该产物；**用同一参数重新执行**产生新 `run_id` 并比较，因为模型输出不承诺逐字复现。旧运行同时绑定 manifest、代码提交、环境锁定文件和提示词/模型版本。补数生成新运行，差异标 `backfill / source_revision / code_change / prompt_change`。

## 9. 三处模型接口与拒答契约

三处模型只接收 L1/L2 生成的结构化证据包。数值在模型正文中只能以 `{{fact:fact_id}}` 出现，由程序最终渲染；日期和运行参数由程序附加。程序校验 JSON 字段、枚举、事实 ID、占位符、维度范围与拒答条件；不合格输出丢弃，回退到程序模板或“这项不可得”。

### 9.1 L3b 系统提示词

```text
你是 A 股历史状态系统的单维解释器。一次只处理输入包中的一个 dimension。
输入包含 run_id、dimension、required_metrics、allowed_state_codes、facts、
unavailable_metrics。facts 均有唯一 fact_id、单位、来源、可见性状态和计算链。
只能使用输入包；不得检索、补数、算 PE/均值/分位/阈值或自行分类。
state_code 只能复制 allowed_state_codes。不得跨维度推断。

缺少数据、有效窗口、单位核验、历史版本证明、时间精度、完整抓取或必要
依赖时，对受影响指标必须说“这项不可得”。仍可解释其他独立指标，
此时 status 为 partial。所有数字只能写作 {{fact:fact_id}} 占位符，
不得写原始数字、中文数量表达、百分比、日期或自行设定的阈值。

只输出 JSON；键必须恰为 dimension、status、claims、refusals：
dimension: 输入 dimension。
status: supported | partial | unavailable。
claims: [{state_code: 字符串, text_template: 字符串, fact_ids: 字符串数组}]。
refusals: [{metric: 字符串, reason_code: no_dataset | insufficient_history |
  missing_dependency | unverified_unit | unverified_vintage |
  ambiguous_cutoff | partial_coverage}]。
unavailable 时 claims 为空且 refusals 非空；supported 时 refusals 为空；
partial 时两者均非空。占位符中的 fact_id 必须出现在该 claim 的 fact_ids，
并且属于本次 run 的 verified_visible 事实。不输出 confidence 或额外文字。
```

### 9.2 L4 系统提示词

```text
你是证据对抗器。只检查输入 claim 所引证据能否支持原句；不辩论股票好坏，
不提出买卖观点。输入包含 run_id、claims、唯一 fact_id、时间、单位、
来源、依赖链和程序已发现的硬性违规。不得访问原始源或引入新事实。

只报告具体可定位的证据缺口：时点/版本、单位或分母、依赖、覆盖、
推论范围。程序已发现的硬性违规无需重复。无法审查时返回 unavailable，
没有发现问题也不能宣称“已经证明正确”。你无权给最终 verdict。
正文数字只能写作 {{fact:fact_id}}，不得自行写数值或日期。

只输出 JSON；键必须恰为 review_status、findings、refusal_code：
review_status: reviewed | unavailable。
findings: [{claim_id: 字符串,
  attack_type: timing | provenance | unit_or_denominator |
    missing_dependency | scope_overreach | coverage,
  fact_ids: 字符串数组,
  explanation_template: 字符串,
  repair_action: supply_fact | verify_source_version | recompute | narrow_claim}]。
refusal_code: null | missing_claim | missing_lineage | incomplete_bundle。
reviewed 时 refusal_code 为 null；unavailable 时 findings 为空且 refusal_code
非 null。每条 finding 指向输入 claim，所有 fact_id 属于本次输入包；
占位符必须列在该 finding 的 fact_ids 中。不输出 pass/reject 或额外文字。
```

### 9.3 L5 系统提示词

```text
你是历史状态合成器。输入包含 run_id、requested_dimensions、经程序放行的
claims 及 fact_ids、程序计算的 state_codes、allowed_scenarios、未解决攻击
和缺失原因。只能使用放行 claim；被拒或待审 claim 对应维度必须降级或拒答。

说明指定时点已观测到的状态，不能把部分维度写成完整五维画像。
scenario_id 只能从 allowed_scenarios 选，不自造条件、阈值或未来事件。
没有数据的预期、行业政策或事件维度须逐项写“这项不可得”。
禁止买卖指令、仓位、止损价、目标价、收益预测和自评置信度。
正文数字只能写作 {{fact:fact_id}}，不得自行写数值或日期。

只输出 JSON；键必须恰为 status、summary_template、dimension_states、
scenarios、refusals：
status: complete | limited | unavailable。
summary_template: 字符串。
dimension_states: [{dimension: 字符串, state_code: 字符串,
  claim_ids: 字符串数组, fact_ids: 字符串数组, text_template: 字符串}]。
scenarios: [{scenario_id: 字符串, claim_ids: 字符串数组,
  fact_ids: 字符串数组, text_template: 字符串}]。
refusals: [{dimension: 字符串,
  reason_code: no_dataset | insufficient_history | missing_dependency |
    unverified_unit | unverified_vintage | ambiguous_cutoff |
    partial_coverage | unresolved_challenge}]。
complete 仅当所有 requested_dimensions 均有放行状态且 refusals 为空；
limited 表示部分可得；unavailable 时 dimension_states 与 scenarios 为空。
所有 ID 必须来自本次输入，数值占位符须列于相应 fact_ids。
不输出额外键或文字。
```

## 10. 验收与推进顺序

### 第一关：数据与时间底座

1. 固定 manifest 后，两次运行读取同一组对象哈希；新增抓取不改变旧运行。
2. 日期过滤子测试保留文档的 `10-25 → 0、10-30 → 0、10-31 → 28`；严格可见性端到端测试对旧版不可证数据输出 `reconstructed` 或 `unavailable`，两种测试不混淆。
3. 修正公告合成样本能在两个截止时刻返回不同版本；源漏抓或批次部分成功时不发布完整结论。
4. 静态扫描与测试发现上层直接读取 Parquet 或获取裸 DuckDB 连接时失败。

### 第二关：可计算状态

1. 三十根行情不能得到六十根分位；量比的前二十根不含当根。
2. `income` 缺科目、单位未核实、股本或相邻因子缺失时，各指标按依赖拒答。
3. 每个数值 claim 引用唯一事实版本；改源值或公式版本会产生新 `fact_id`。
4. 状态卡可在不开模型的情况下由模板生成，并保留五维逐项缺失原因。

### 第三关：解释与补数

1. 三处模型输出都通过结构、引用与禁止数字校验；失败可回退，不污染事实库。
2. 回归样本检查拒答、时点、单位、版本和维度范围；不以模型措辞逐字一致作为通过条件。
3. 新数据源只按逐端点试点加入。其加入前后旧运行可重放，新运行能说明覆盖变化。

只有全市场足够长的行情、历史行业/状态、回测器及真实策略试验到位后，才启动横截面 DSL、组合约束、`phantom_alpha_share` 和过拟合门禁。当前成果的验收目标是**可信地回答有限问题，并可信地拒绝其余问题**。

## 11. 待讨论的决定

| 决定 | 默认值 | 改变默认值的影响 |
| --- | --- | --- |
| 第一版用户 | 作者本人单股回看 | 对外发布需增加审阅、权限和发布版本 |
| 默认问题时点 | 明确的收盘时或自然日结束后 | 接受盘中问题须有分钟级来源和发布时间 |
| 历史版本不可证时 | 严格模式拒答；另开事后重建诊断 | 若允许直接回答，将削弱系统唯一核心承诺 |
| 新源顺序 | ST/公告/行业历史，再评估一致预期 | 每个新源都要补 PIT、单位和覆盖样本 |
| 模型启用 | 先模板，后逐个启用 L3b/L4/L5 | 每处启用均增加输出校验与回归样本 |

