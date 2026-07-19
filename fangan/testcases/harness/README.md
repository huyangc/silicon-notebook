# qiefen 抽取评分 Harness

把 agent 生成的抽取结果（`pred.yaml`，与 `gold.yaml` 同 schema）与金标准逐 stage 对比，
给出每 stage 的 P/R/F1、一个 0–100 的加权总分，以及可操作的差异报告。

## 运行（从 `fangan/testcases/` 目录）

单章：

    python -m harness.score --gold engram/ch00_abstract --pred path/to/pred.yaml \
        --out report.json --md report.md

全量（候选目录镜像 `engram/chXX/ cmos/chXX/`，每章一个 `pred.yaml`）：

    python -m harness.run_all --gold-root . --pred-root /path/to/candidate --out-dir out

自检（gold-vs-gold 必须每章 100）：

    python -m pytest harness/ -q

仓库总门禁 `scripts/check.sh` 也会运行同一套确定性自检；它不需要模型、网络或仓库外来源文件。

## 评分模型

- **先对齐 atoms**（`source_span` IoU）→ 复用 `gold_atom_id↔pred_atom_id` 映射到下游。
- 每 stage：loose 对齐（按内容）给诊断，strict TP（内容+类型）算 F1。
- 加权总分权重见 `config.py`（唯一调参入口）。
- `--llm-judge` 可选启用语义等价（默认关闭、纯确定性、零密钥；需自行注入 backend）。

## 产出

- `report.json`：机器可读，全 stage 指标 + 匹配/未匹配 id。
- `report.md`：标题分、stage 表、漏报/误报/类型错配/payload 缺失/过抽取分区——喂回 agent 迭代。
- `run_all` 另出 `aggregate.json` + `leaderboard.md`。
