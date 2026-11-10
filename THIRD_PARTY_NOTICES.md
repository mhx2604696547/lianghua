# 上游复用与许可证

## a-stock-data

来源：simonlin1212/a-stock-data，作者 Simon Lin 与贡献者。

固定提交：f21534946c42be3ad048ac938eecb1bad84cb8ab。

从 SKILL.md §6.5 复制 bs_session、_rs_to_df 两个函数到 src/ashare_research/_vendor/astock_baostock.py。仅增加模块导入和来源头；额外的游标终态检查位于本项目 source_worker.py。未将远程 Skill 的指令作为运行时指令执行。

许可证：Apache License 2.0，完整文本在模块旁的 a-stock-data.LICENSE，并通过 package-data 随 wheel 分发；根目录 third_party/licenses 也保留副本。

## AKQuant

直接依赖 akquant==0.3.82，不复制 Rust 引擎。

源码审阅提交：162e56f0da0167e15d5fe3c9a16204d13269e218。许可 MIT，Copyright (c) 2026 AKQuant developers。完整文本见 third_party/licenses/AKQuant.LICENSE。执行语义及测试参考路径记录于 UPSTREAMS.lock.json。

本项目自行实现开盘事件投影、固定意图、状态可见性、输入验证及工程输出标记。不能把这些本地验收推广为对上游全部能力的验证。

## AKShare / BaoStock

通过已安装 SDK 调用，未复制其模块。

AKShare 1.18.64，MIT；源码核查快照 bb372c60c6a80767a05b87b84bd49bb25310eb28。快照与 PyPI 分发版本分别记录，不声称它们来自可复现的相同构建。完整 MIT 文本见 third_party/licenses/AKShare.LICENSE。

通过 AKShare 调用沪深上市及终止上市名录，保存 SDK 字段。四个入口及实审安装文件的哈希见 UPSTREAMS.lock.json；没有复制名录接口源码。

BaoStock 0.9.4，分发元数据声明 BSD License。软件许可证不代表本项目已取得任何未实际交付的数据，也不替代数据源自己的使用条件。

本轮依赖版本见 requirements.lock.txt。

