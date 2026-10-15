---
level: change
audience: admin
title: 升级后首次整理知识图谱会重新调用模型
---
模型回复格式精简后，原有的模型回复缓存失效：升级后第一次整理知识图谱的耗时和费用接近从头整理，请安排在空闲时段。插件动作不能再命名为 query、keywords 或 outline_sections，动作和参数都不能命名为 source_queries 或 frame_assignments，否则注册失败。
