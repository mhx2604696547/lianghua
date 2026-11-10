# A 股短线研究工作台：本地原型

目前可采集日线、日历和交易所证券名录，可用固定的数据清单重放日频证据，也可用合成样本验证撮合。正式市场策略回测尚未启用。

项目设计、实现与后续 Agent 工作统一遵守 [项目底层约束](PROJECT_RULES.md)；[AGENTS.md](AGENTS.md) 为工作入口。目标要求与当前实测能力分别记录。源码、文档和测试已上传至[项目 GitHub 仓库](https://github.com/mhx2604696547/lianghua)；本机生成的 `artifacts/`、虚拟环境和凭据文件不随代码提交。DSH 交付由主审核验后再提交和推送。

## 已复用的实现

| 部分 | 上游 | 本地入口 |
| --- | --- | --- |
| BaoStock 登录、错误检查与结果转换 | a-stock-data，固定提交，Apache-2.0 | src/ashare_research/_vendor/astock_baostock.py |
| 日线、历史 ST/停牌字段、证券主表、日历 | BaoStock 0.9.4 SDK | sources.fetch_baostock |
| 新浪分红历史 | AKShare 1.18.64 SDK | sources.fetch_sina_actions |
| 沪深交易所上市和终止上市名录 | AKShare 1.18.64 SDK，保留原始字段 | sources.fetch_exchange_roster |
| 账户、订单、费用、T+1、结果统计 | AKQuant 0.3.82，MIT | engine.run_open_proxy |
| 原始结果留存与时点读取 | 本项目的必要适配 | snapshots.freeze / pit.DailyPIT |

上游审阅提交、原文件路径与复用方式见 [UPSTREAMS.lock.json](UPSTREAMS.lock.json)。复制代码的许可证与说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。SDK/引擎直接作为依赖使用，没有复制整个框架。

## 本机已经完成的验证

2026-11-07，在独立的 .venv 中实测：

- BaoStock：600519.SH，2026-09-14 至 2026-09-18，共 5 行未复权日线，包含 preclose、tradestatus、isST、成交量和成交额。
- BaoStock：同股主表 1 行，上市日期可读；空退市日期保持为空。
- BaoStock：上述区间交易日历 5 行。
- AKShare / 新浪：同股原始分红记录 31 行。
- 34 项测试通过，包括费用手算、T+1、现金不足、开盘涨跌停、跨周末、迟到状态拒绝、H/L/C/V 变异不影响开盘成交、数据哈希与历史版本拒答。
- Python 审阅已通过；wheel 构建成功，包含复制模块的完整 Apache-2.0 许可证。

这些实测只证明对应样本与当前接口可用，不代表全市场历史覆盖、修订链或公司行为会计已验收。此前 E 盘 demo 湖保持原样，本轮新样本在本工作区 artifacts/snapshots。

2026-11-09（北京时间）复核：**40 项测试通过**。在同一份日线和日历清单上重演了六个真实快照读取场景，结果与[首次诊断归档](artifacts/runs/pit-diagnostic-d473916b63c54c909a787792cfdd5299/run.json)一致。重新采集并核验了四份交易所名录：[实测记录](artifacts/w1-w2-verification-4c1c54fa8963427e9f7859587f5fc7c5/verification.json)。

| 名录 | 本轮返回 | 仍需确认 |
| --- | ---: | --- |
| 沪市主板 A 股名录 | 1,700 行 | 当前名单不能代表历史成分 |
| 沪市终止上市名录 | 158 行 | 6 个重复公司代码；接口含沪市 A/B 股范围，SDK 日期列仍叫“暂停上市日期” |
| 深市 A 股名录 | 2,906 行 | 同时含主板和创业板，板块是采集时的标签 |
| 深市终止上市名录 | 212 行 | 有上市、终止上市日期，没有历史板块字段 |

四份名录均保留 SDK 返回的字段和值，`membership_ready=false`。行数描述的是本次响应，不是历史覆盖率。对照来源：[上交所暂停/终止上市名录](https://www.sse.com.cn/assortment/stock/list/delisting/)、[深交所已退市公司公告](https://www.szse.cn/www/marketServices/message/period/delisted/)。

## 运行

以下命令在普通 Windows PowerShell、D:\lianghua 中执行：

~~~powershell
$env:UV_CACHE_DIR = 'D:\lianghua\.cache\uv'
uv venv --python python .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.lock.txt
$env:PYTHONPATH = 'D:\lianghua\src'
$env:PYTHONUTF8 = '1'

.venv\Scripts\python.exe -m pytest -q --basetemp .cache\pytest-local
.venv\Scripts\python.exe -m ashare_research.cli smoke
~~~

已有 .venv 时直接使用后面的命令。也可用 uv pip install --python .venv\Scripts\python.exe --no-deps --no-build-isolation -e . 注册命令入口。

数据试点：

~~~powershell
.venv\Scripts\python.exe -m ashare_research.cli ingest-baostock --dataset daily_bars --symbol 600519.SH --start 2026-09-14 --end 2026-09-18 --timeout 20
.venv\Scripts\python.exe -m ashare_research.cli ingest-baostock --dataset instruments --symbol 600519.SH --start 2026-09-14 --end 2026-09-18 --timeout 20
.venv\Scripts\python.exe -m ashare_research.cli ingest-baostock --dataset trading_calendar --start 2026-09-14 --end 2026-09-18 --timeout 20
.venv\Scripts\python.exe -m ashare_research.cli ingest-sina-actions --symbol 600519.SH --timeout 20
& '.\.venv\Scripts\python.exe' -m ashare_research.cli ingest-exchange-roster --dataset sse_delisted_raw --timeout 20
~~~

SDK 请求在有超时的独立子进程中运行，固定来源，失败不自动换源。日期范围不适用于证券主表，主表是本次抓取的状态与日期字段快照。

每次抓取创建新的目录，保存 raw.csv（SDK 原始字段值）、data.parquet 和最后原子发布的 manifest.json。这里的“原始”指 SDK 返回值，并非交易所报文或 HTTP 原始响应。历史回填统一标 reconstructed；fetched_at 不是历史 available_at。

交易所名录支持 `sse_listed_raw`、`sse_delisted_raw`、`szse_listed_raw`、`szse_delisted_raw` 四种固定取数操作。采集结果记录端点、参数、SDK 版本、重复代码数和不合口径的代码数。名录不能直接替代日线、ST/停牌事件或历史股票池。

## 用已归档快照诊断时点

~~~powershell
$pin = & '.\.venv\Scripts\python.exe' -m ashare_research.cli pin-daily-cut --calendar 'artifacts/snapshots/trading_calendar-3797071d3f8340d782b655d077bad2bd/manifest.json' --bar '600519.SH=artifacts/snapshots/daily_bars-8f55c03555944c28b39ccbb97392e1d1/manifest.json'
$cutId = ($pin | ConvertFrom-Json).lake_cut_id
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-daily-cut --cut-id $cutId --purpose decision --symbol 600519.SH --start 2026-09-14 --end 2026-09-17 --as-of 2026-09-18T18:00:00+08:00 --mode reconstructed
~~~

- 清单 ID 是清单内容的 SHA-256，固定日历、每只股票的清单路径和清单哈希。读到的 Parquet 字节先验哈希再解码，原始 CSV 也校验。补数据产生新清单；改写旧快照会被拒绝。
- `decision` 只读截止日前的完整日线窗口。日历缺日或日线少行时拒答。 `execution` 在日频代理的开盘时刻只返回标的、日期和开盘参考价。 `outcome` 只读取评价截止日前的完整日线窗口。
- 结果含清单 ID、用途、截止时刻、来源与快照哈希。无历史版本证据时，默认严格模式返回 `unavailable`。这是诊断入口，由可信的运行代码调用；后续策略和模型只接收各自的切片。

## 重放历史上市候选

`build-listing-evidence` 使用已归档的沪深名录、上交所按证券类别分开的原始响应和人工核对的公告事实。本轮索引保留 4,976 条来源记录。源文件、公告事实和深市代码区间表都有哈希记录。验证覆盖 12 个真实日期场景，原有 6 个日线诊断可重放，全量 43 项测试通过。[本轮验证](artifacts/runs/listing-review-d378370941ff41ce80f34ae1a0f4411b/run.json)

~~~powershell
$listingPin = & '.\.venv\Scripts\python.exe' -m ashare_research.cli pin-daily-cut --listing-evidence 'artifacts/snapshots/listing_evidence-3d9d820badc040f7aaf3b7b6e07269b3/manifest.json'
$listingCut = ($listingPin | ConvertFrom-Json).lake_cut_id
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-listings --cut-id $listingCut --session 2025-07-25 --as-of 2025-07-25T18:00:00+08:00 --mode reconstructed --symbol 600190.SH --symbol 900952.SH
~~~

查询以证券代码区分 A 股和 B 股。摘牌公告确认了锦州港两只证券在 2025-07-25 摘牌，原名录里的 2025-07-28 保留为源字段，并与退出登记公告核对。已核对的终止日期用于结束上市区间，其余名录终止日期只作线索。[上交所摘牌公告](https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250718_10785880.shtml) · [退出登记公告](https://www.nesc.cn/plat_files/upload/png_upload/20250807/202508071754544960221.pdf)

深市主板候选按 2021 年两板合并公告和细分代码表交叉限定，合并前的板块与未核实的子区间保留未知。公告只有披露日时，`next_shanghai_calendar_day` 从北京时间次日开始考虑这份证据，状态还须进入已核对的生效区间。[合并公告](https://www.szse.cn/disclosure/notice/general/t20210331_585343.html) · [深交所代码区间表](https://www.szse.cn/marketServices/technicalservice/doc/P020260306733846760075.pdf)

`--session` 在基础诊断中表示要核对的日期。`excluded` 用于样本的新开仓资格，`candidate` 是候选上市状态。回执的 `membership_ready=false` 保留覆盖未验收的状态。

## 核对交易日历与上市日龄

`build-trading-calendar` 将归档交易日期与沪深交易所的休市公告逐日对照。任一日期不一致时，构建失败并返回差异日期。已封存的窗口覆盖 2021-01-01 至 2026-11-08，共 1,415 个交易日。清单记录 12 份公告及交易日期输入的哈希。 [日历清单](artifacts/snapshots/trading_calendar-72081395f6b04c8dbc3bc16496c0766e/manifest.json)

~~~powershell
$agePin = & '.\.venv\Scripts\python.exe' -m ashare_research.cli pin-daily-cut --listing-evidence 'artifacts/snapshots/listing_evidence-3d9d820badc040f7aaf3b7b6e07269b3/manifest.json' --calendar 'artifacts/snapshots/trading_calendar-72081395f6b04c8dbc3bc16496c0766e/manifest.json'
$ageCut = ($agePin | ConvertFrom-Json).lake_cut_id
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-listings --cut-id $ageCut --session 2021-07-02 --as-of 2021-07-02T18:00:00+08:00 --mode reconstructed --symbol 003041.SZ --min-listed-sessions 60
~~~

日龄情景从上市首个交易日起算，计入已经结束的查询交易日。窗口截断时，结果标为 `lower_bound`。只有完整窗口内的计数低于门槛，才会判为新股并排除。例中的 60 个交易日仍是暂定参数。真爱美家在 2021-07-01 为第 59 日，次日为第 60 日，次日的入池状态仍取决于 ST 与可交易性证据。 [深交所上市公告](https://www.szse.cn/aboutus/trends/news/t20210406_585423.html)

本轮全量 46 项测试通过，五个真实日龄场景和三个拒答场景通过。此前的上市候选和日线切片也重放一致。 [验证记录](artifacts/runs/calendar-age-review-12a2e172996b4720988694ac50521efa/run.json)

## 重放逐票风险与交易状态

`read-listings --check-session-status` 在 L1 内按日期对照已核验的公告、深交所简称变更和已固定的 BaoStock 日线状态。简称变更归档提取了 492 条带 ST 或 *ST 的生效日记录。仅在生效日核对，且要求截止日晚于生效日。缺记录不等于正常。已核验公告按各自的可见日和生效区间处理。 [风险标记清单](artifacts/snapshots/risk_name_events-9ed5842015e54982ac950d8a521a8ab4/manifest.json) · [华铁股份公告](https://disc.static.szse.cn/disc/disk03/finalpage/2024-07-05/84dc441e-9877-43c6-a25d-840e6394572c.PDF)

~~~powershell
$statusCut = '2839434b4984fc3a68ff0f62bca1943096c4c30669541b10a5e5307b1ce860aa'
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-listings --cut-id $statusCut --session 2026-09-17 --as-of 2026-09-18T18:00:00+08:00 --mode reconstructed --symbol 600519.SH --min-listed-sessions 60 --check-session-status
~~~

同日只凭日线无法判定状态。来源冲突或缺失时保留待判定项，回执仍是 `diagnostic_only`、`membership_ready=false`。样本返回的 `eligible` 表示已通过这次历史诊断。日线状态没有开盘前可见性的证明。状态证据单独标记输入性质，缺标记的旧快照保持 `unspecified`。

本轮全量 48 项测试通过。重放了 8 个状态场景、17 个原有上市和日龄场景及 6 个日线场景。 [验证记录](artifacts/runs/session-status-final-c50261126ffb4f3bac6d25f1de471542/run.json)

## 导入历史日线归档

已下载并校验 `FREE_DATA_v0` 的原始归档。实扫得到 5,495 个非空沪深证券分片、64 个空分片，共 14,496,343 行未复权日线。与本地已核验日历重叠的窗口为 2021-01-01 至 2026-09-24。在每票首末返回日期之间，未发现缺交易日或多余日期。四个抽查日覆盖了现有名录标出的主板候选，名录未知项单列。 [发布版本](https://github.com/noblechang/a-share-data/releases/tag/FREE_DATA_v0) · [源验收](artifacts/w1-free-data-v0-785130b158c649418435eec67ce5e54b/source_audit.json)

~~~powershell
$archive = 'artifacts/w1-free-data-v0-785130b158c649418435eec67ce5e54b/a-share-raw-v0.tar'
& '.\.venv\Scripts\python.exe' -m ashare_research.cli import-free-data-v0 --archive $archive --symbol 600519.SH --symbol 600190.SH --symbol 000976.SZ
$archiveCut = 'ccc941b38cb26deb432e56d2f5004138ba3c557396fd4052c4680521ab01cacf'
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-listings --cut-id $archiveCut --session 2025-07-24 --as-of 2025-07-25T18:00:00+08:00 --mode reconstructed --symbol 600190.SH --min-listed-sessions 60 --check-session-status
~~~

导入回执保留每只请求证券的结果。快照记录发布归档和原始分片哈希。存在空或缺失分片时，命令退出码为 2，已成功导入的快照可继续用于诊断。示例清单固定了本轮导入的三只股票，使用已核验的交易日历、名录和公告。 [导入回执](artifacts/runs/free-data-v0-import-a730481598d849d2b38353c81491a2b5/import.json)

当前用途为 `reconstructed` 历史诊断。决策和评价只读取早于截止日的完整日线。锦州港 2025-07-24 的停牌记录仍带正的开盘字段，开盘成交仍需单独的状态和价格限制证据。本轮 50 项测试通过，五个归档状态场景及原有时点场景重放通过。 [上交所摘牌公告](https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250718_10785880.shtml) · [回放验证](artifacts/runs/free-data-v0-review-5db2e7c217ad4b4d8240eb5bbfd41a71/run.json)

归档的全包审计脚本、逐票覆盖和删去逐行行情的回执现已固定到[仓库证据目录](evidence/free_data_v0/README.md)。本机重算原始包与覆盖表哈希一致；5,495 个可解码分片中，审核窗口内按各分片首末日计算的内部缺失交易日为 0，但停牌占位、历史股票池和盘后/盘前可见性仍分别待核验。这个覆盖数字不能解释为可交易股票数。原审计接触了市场候选配置的 2026 年留出行情，该区间已[失去独立留出资格](plans/HOLDOUT_STATUS.md)。

贵州茅台 2026-06-26 除息的参考昨收为 1,184.08 元，上一交易日收盘为 1,212.10 元。用上一交易日收盘价减去公告中的每股现金红利，四舍五入至分，得到归档中的参考价。公司行为入账仍待验收。 [分红公告](https://www.moutaichina.com/mtgf/attachDir/2026/06/2026062214215259014.pdf) · [样本对账](artifacts/w1-free-data-v0-785130b158c649418435eec67ce5e54b/maotai_reference_control.json)

## 盘前检查与历史价格限制

`read-preopen` 使用 `pin-daily-cut --opening-evidence` 固定的公告和规则。回执保留请求的盘前时间，已有 AKQuant 回调样本为北京时间 09:14:59.999999999。仅有披露日期的公告从北京时间次日开始纳入检查。回执分别记录公告可见日期、已知停牌或摘牌、交易阶段、适用规则和参考价来源。 [盘前证据清单](artifacts/snapshots/opening_evidence-4ced6d7c9b2c4a40a19574565161f9b5/manifest.json)

~~~powershell
$openingCut = '272a9764bd49ffa758c80036ee9be262ea0c054e889ee0b0035e7bef8b9803c8'
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-preopen --cut-id $openingCut --symbol 600190.SH --session 2025-07-21 --as-of 2025-07-21T09:14:59.999999999+08:00 --side sell --mode reconstructed
& '.\.venv\Scripts\python.exe' -m ashare_research.cli read-preopen --cut-id $openingCut --symbol 600519.SH --session 2026-06-26 --as-of 2026-06-26T09:14:59.999999999+08:00 --side buy --mode reconstructed
~~~

沪深主板规则经审核覆盖 2023-04-10 至 2026-11-08。主板风险警示股票的涨跌幅比例在 2026-07-06 从 5% 改为 10%。确定参考价和比例后，按分四舍五入，并处理最小变动单位。缺少交易阶段证据时，普通主板上下限只在条件情景中展示。 [沪深首批上市安排](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20230404_5719112.shtml) · [上交所规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml) · [深交所规则](https://www.szse.cn/lawrules/rule/allrules/bussiness/t20260424_620190.html)

新清单下，锦州港停牌日的占位开盘价和退市整理首日的无日涨跌幅场景均被执行读取拦截。华铁股份的风险警示拦截新开仓，退出仍由持仓和执行条件决定。茅台除息参考价使用前一交易日收盘和已披露的现金红利计算。盘前正向可交易证据缺失时，结果保持 `unavailable`。 [锦州港首日公告](https://static.cninfo.com.cn/finalpage/2025-06-28/1224016614.PDF) · [停牌公告](https://static.cninfo.com.cn/finalpage/2025-07-19/1224216580.PDF)

本轮 56 项测试通过，10 个真实盘前场景、3 个时点拒答及旧清单均完成验证。当前输出为历史重建诊断，市场回测仍待正向盘前状态和核算验收。 [验证记录](artifacts/runs/opening-control-review-d5b3b6cbe6914db8b8e146e4ce10935e/run.json)

## 执行适配的关键约定

AKQuant 的普通 NextOpen 撮合仍会读取 Bar 的 H/L/V，因此真实整根日线不直接交给该撮合路径。本原型只给它开盘事件：OHLC 全等于 open，volume=1 是无容量含义的执行载体，volume_limit_pct=0。

逻辑开盘为 09:30 Asia/Shanghai。上游将点 Bar 按终点前 1ns 归类；载体因此使用 09:30+1ns，避免落入集合竞价边界。元数据明确记录这 1ns 偏移，不表示读取了 1ns 后价格，也不证明集合竞价队列成交能力。

订单来自前一会话已冻结的固定股数、限价意图，在上游盘前回调提交。状态与价格上下限还必须早于实际提交回调，不能仅早于开盘。单日订单未成交后，重提退出必须是新的、可追溯意图。

输出是 opening_prices 估值的 daily_proxy，包含订单、成交、交易、开盘估值及配置。不能当作收盘净值。合成价格、资金和费用仅用于工程验证，不是策略或用户账户参数。上游订单/成交 UUID 每次可能不同；重演归档与比较规范化成交字段分别处理。

## 合成账户与结算回放

`account-smoke` 将持仓、订单和应收股息放在同一交易日历中推进。持有期从实际买入成交日开始计数。到期遇停牌、跌停或部分成交，保留剩余股数并在后续交易日生成新的退出单。持仓缺可靠开盘估值时，当日净值留空，现金和股数照常对账。

~~~powershell
& '.\.venv\Scripts\python.exe' -m ashare_research.cli account-smoke
~~~

本机 AKQuant 分红试验显示，单一事件日按该日持仓入账，无法表示登记日持股后、到账前卖出仍有权益的情况。新账户路径单独保存登记、应收与现金释放。现金按税前情景计算，假设在发放日之后第一个交易日开盘前可用。买入订单在盘前预留限价金额和费用。同一场开盘的卖出款在成交后才进入账户。 [AKQuant 本机对照](artifacts/w3-akquant-dividend-probe-f191b83cb4ed4cb9a88a1a60a2a4c81f/probe.json) · [发行人分红公告](https://www.moutaichina.com/mtgf/attachDir/2026/06/2026062214215259014.pdf)

固定样例按实际成交核对了现金、股数和应收。计划持有 1 日的仓位在两次退出受阻后实际持有 3 日，缺价日保留持仓与空净值。样例还跨过一个合成费率切换日。普通往返的手续费和期末现金与 AKQuant 对上，62 项测试通过。 [样例运行](artifacts/runs/synthetic-account-da05b7d739d244a8baf6c7ae168f6c7e/run.json) · [对账记录](artifacts/runs/account-path-review-0d79347d888448e9bead62f224e21632/run.json)

## 冻结并运行量价试验

`freeze-study` 校验配置并保存不可变内容。试验开始时先向 SQLite 登记请求，绑定配置、数据清单、分区、持有期、代码和依赖。同一请求号携带不同输入会被拒绝。相同请求重复提交时校验归档哈希，再返回原结果。用新请求号可独立复算。对已记录的失败或缺证可显式 `--retry`，进程硬中断留下的未结束尝试尚不能自动恢复。

~~~powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
$study = & '.\.venv\Scripts\python.exe' -m ashare_research.cli freeze-study --spec specs/synthetic-volume-study-v1.json | ConvertFrom-Json
$fixture = & '.\.venv\Scripts\python.exe' -m ashare_research.cli make-study-fixture --spec-id $study.spec_id | ConvertFrom-Json
$request = "volume-dev-h3-$($fixture.lake_cut_id.Substring(0,12))"
& '.\.venv\Scripts\python.exe' -m ashare_research.cli run-study --spec-id $study.spec_id --cut-id $fixture.lake_cut_id --request-id $request --partition development --hold-sessions 3
~~~

规则比较当日与上一交易日收盘，以及当日成交量与此前 20 个交易日均量。入选后，在同一个机会日从合成候选池按固定种子选对照。双方独立使用相同初始资金、持有期、限价口径和费用，由账户内核处理成交、退出与估值。允许重叠持仓。若计划退出越过分区末日，该日不启动新信号。结果包含每次扫描、选中标的、L1 回执、订单和账户明细。 [合成配置](specs/synthetic-volume-study-v1.json)

开发试验有 4 个双方均买入成交的日期，净值差低于预设门槛。验证试验有 3 个，低于最低样本要求。重复执行得到相同信号、订单和账户结果。留出请求在打开数据清单前被拒绝，合成生成器也只生成到验证分区末日。这里的封存由研究入口执行，尚未建立真实数据的物理隔离和解封流程。 [开发报告](artifacts/runs/study-31778ac210c8419599aa4b1738b61437/report.md) · [验证报告](artifacts/runs/study-bad13ffcf61643a8bc1ab2cd5cfa3c8f/report.md)

真实候选配置可通过同一个入口检查已有快照。程序按同日盘后和次日提交前分别读取三只证据探针。盘后日线、正向可交易状态和适用价格限制均缺证，历史股票池尚未验收。该请求返回退出码 2 并归档诊断，合成试验保持 `engineering_only`。常规 Windows 环境回归 66 项通过。 [候选配置](specs/mainboard-volume-candidate-v1.json) · [真实数据预检](artifacts/runs/study-8405a6a11f5746e78f3f685a9b8bab69/report.md) · [本轮验证](artifacts/runs/study-trial-review-5900ddf799584a9eaaf6ca0b0fe68f55/verification.json)

## 尚未启用的正式能力

- 真实研究运行必须接上完整 L1、历史股票池与冻结试验规范；当前 run_open_proxy 明确拒绝 data_kind=market。
- DailyPIT 已替代旧的单快照直读入口。当前仅支持显式 reconstructed 的诊断读取，决策与事后评价仍排除截止日整日。开放同日盘后数据需要来源的完成时间和版本证据。
- 历史板块、上市与终止上市的证券身份区间，以及按时点过滤 ST 和新股的历史股票池，尚未验收。
- ST/停牌日频标志不能自动当作盘前已知状态。生产价格限制要有可验证的规则、参考价与可见时间。
- 账户路径仍为 `synthetic`。真实公告到权益入账的完整链路、分红税、送转和退市结算待验收。新浪原始分红保留 `ledger_ready=false`。
- 账户路径已验证合成费率按生效日切换。真实历史费率、实际收费舍入口径和适用范围待验收。
- 本机已安装 DeepSeek Harness 桌面版 `0.2.1-rc.5`，尚未接入本项目工程 Worker 或正式研究调度。工程任务拟先用 `headless` 隔离试点，由主审验收；正式调度仍须固定任务接口和独立验收。[接入设计](plans/HARNESS_INTEGRATION.md)
- 上游与 pandas 3.1 存在弃用提示，当前锁定组合已跑通，升级需要重新验收。
- 当前受限执行沙箱未提供 CPU 架构信息，Polars 会拒绝启动。全量测试在正常 Windows 环境完成，未关闭 CPU 检查。

正式建设顺序继续见 [研究任务计划](plans/A_SHARE_SHORT_TERM_RESEARCH.md)。

