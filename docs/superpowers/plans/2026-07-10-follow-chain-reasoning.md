# Follow-chain implementation plan

1. 新增 `kg/follow_chain.py`：类型规则、scope 合并、路径模型、可信度和方向保持 renderer。
2. 在 repository + RetrievalService 增加两轮有界 SQL primitive，严格复用既有
   source/target 索引且保持 schema v9/历史数据不变，并补新关系 evidence 的
   SourceElement 绑定。
3. 扩展 reflect action 协议和 `ReasoningRetriever`：去重/上限、chains 累积、trace、
   最终证据保护。
4. 把 chain relation anchors 接入 Ask 与 Deep Report 合成；rejected 关系不再进入普通
   answer context。
5. 增加前端 trace 标签/详情和 relation anchor 的安全展示。
6. 补纯逻辑、repository、orchestrator、answer、stream/frontend 回归测试。
7. 同步 README / README_zh / AGENTS / fangan_done，运行全量检查和前端构建。
8. 合并最新 master，复测后提交、推送并创建 PR。
