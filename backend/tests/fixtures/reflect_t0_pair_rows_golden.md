# reflect T0 轨迹聚合

- 输入行数:7
- 分组维度:`consumer,mode,effort,kg_in_scope,policy_version,optimization`
- 分位数门槛:`min_samples=1`

## 分组概览

| consumer | mode | effort | kg_in_scope | policy_version | optimization | n_runs | reflect_turns(n/均值) | total_ms(P50/P95) | stale_breaker | trace_truncated |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ask_single | reasoning | deep | False | v2 | off | 1 | 1/4.0 | 2000.0/2000.0 | 0/0 | 0/0 |
| ask_single | reasoning | deep | False | v2 | prefix_delta_lean | 1 | 1/4.0 | 2500.0/2500.0 | 0/0 | 0/0 |
| ask_single | reasoning | standard | True | v2 | off | 4 | 4/2.75 | 1000.125/99999.0 | 0/0 | 0/0 |
| ask_single | reasoning | standard | True | v2 | prefix_delta_lean | 1 | 1/3.0 | 400.125/400.125 | 0/0 | 0/0 |

### ask_single / reasoning / deep / False / v2 / off

- n_runs: 1
- termination_reason: model_sufficient=1
- termination_inferred: False=1
- status: done=1
- optimization: off=1

### ask_single / reasoning / deep / False / v2 / prefix_delta_lean

- n_runs: 1
- termination_reason: model_sufficient=1
- termination_inferred: False=1
- status: done=1
- optimization: prefix_delta_lean=1

### ask_single / reasoning / standard / True / v2 / off

- n_runs: 4
- termination_reason: budget=1, model_sufficient=3
- termination_inferred: False=3, True=1
- status: cancelled=1, done=3
- optimization: off=4

### ask_single / reasoning / standard / True / v2 / prefix_delta_lean

- n_runs: 1
- termination_reason: model_sufficient=1
- termination_inferred: False=1
- status: done=1
- optimization: prefix_delta_lean=1

## legacy / v2 对照(成对)

(没有成对样本:输入里没有同时带 `question_key` 的两侧 run)

## off / 优化变体对照(成对,同 policy_version)

| question_key | corpus_cell | effort | consumer | mode | trace_source | has_intent_contract | policy_version | variant | off n | variant n | off run_wall_ms(n) | variant run_wall_ms(n) | off model_calls_real(n) | variant model_calls_real(n) | off context_rebuilds(n) | variant context_rebuilds(n) | off assessment_rows_total(n) | variant assessment_rows_total(n) | variant prefix_bytes_median(n) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B-q01 | B_kg | standard | ask_single | reasoning | unknown | True | v2 | prefix_delta_lean | 4 | 1 | 27249.938(n=4) | 1234.0(n=1) | 3.5(n=4) | 3.0(n=1) | unknown(n=0) | unknown(n=0) | unknown(n=0) | unknown(n=0) | 700.0(n=1) |
| B-q02 | B_nokg | deep | ask_single | reasoning | unknown | True | v2 | prefix_delta_lean | 1 | 1 | 5000.125(n=1) | 6250.5(n=1) | 5.0(n=1) | 5.0(n=1) | unknown(n=0) | unknown(n=0) | unknown(n=0) | unknown(n=0) | 880.0(n=1) |

## 逐题配对差值(基线 `off`)

> 格内**先对重复取中位数**,再出 `Δms` 与 `ratio`;rollup 的 `ratio p50` 是逐格配对比值的中位数,**不是**两组独立 P50 的比值(§10.1)。
> 成功配对表可单列,但**不能独自决定上线**(§10.1):先读 `n_success`(跑成的 run 数——它与值那一格的 `n=` 是两个数,后者只数其中量到了这个指标的)、`n_censored`(删失,`cancelled`——那是被截止时长掐断的一刻,不是真实完成耗时)、`n_failed`、`n_unfinished`(`running` 等没跑完的状态,它们的耗时是部分和,不进中位数)与 `n_unpaired`(写侧标了 `paired=false`、没有对臂,压根没进这一格),再读 Δ。

| question_key | corpus_cell | effort | consumer | mode | trace_source | has_intent_contract | policy_version | variant | off run_wall_ms P50(n) | variant run_wall_ms P50(n) | run_wall_ms Δms | run_wall_ms ratio | off total_ms P50(n) | variant total_ms P50(n) | total_ms Δms | total_ms ratio | n_success(基线/变体) | n_censored(基线/变体) | n_failed(基线/变体) | n_unfinished(基线/变体) | n_unpaired(基线/变体) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B-q01 | B_kg | standard | ask_single | reasoning | unknown | True | v2 | prefix_delta_lean | 3000.25(n=3) | 1234.0(n=1) | -1766.25 | 0.4113 | 1000.125(n=3) | 400.125(n=1) | -600.0 | 0.4001 | 3/1 | 1/0 | 0/0 | 0/0 | 0/0 |
| B-q02 | B_nokg | deep | ask_single | reasoning | unknown | True | v2 | prefix_delta_lean | 5000.125(n=1) | 6250.5(n=1) | 1250.375 | 1.2501 | 2000.0(n=1) | 2500.0(n=1) | 500.0 | 1.25 | 1/1 | 0/0 | 0/0 | 0/0 | 0/0 |

配对汇总(分位数受 `min_samples` 约束,与 `n_pairs` 同格):

| policy_version | variant | metric | n_pairs | n_ratio_pairs | Δms p50 | Δms p95 | Δms max | ratio p50 | ratio p95 | ratio max | n_censored(基线/变体) | n_failed(基线/变体) | n_unfinished(基线/变体) | n_unpaired(基线/变体) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v2 | prefix_delta_lean | run_wall_ms | 2 | 2 | -1766.25 | 1250.375 | 1250.375 | 0.4113 | 1.2501 | 1.2501 | 1/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | total_ms | 2 | 2 | -600.0 | 500.0 | 500.0 | 0.4001 | 1.25 | 1.25 | 1/0 | 0/0 | 0/0 | 0/0 |

按 `effort` 分桶(§10.2-3:各主要题型/档位各读自己的配对中位比——全局那一行会把两档相反的偏离抵消掉):

| policy_version | variant | effort | metric | n_pairs | n_ratio_pairs | Δms p50 | Δms p95 | Δms max | ratio p50 | ratio p95 | ratio max | n_censored(基线/变体) | n_failed(基线/变体) | n_unfinished(基线/变体) | n_unpaired(基线/变体) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v2 | prefix_delta_lean | deep | run_wall_ms | 1 | 1 | 1250.375 | 1250.375 | 1250.375 | 1.2501 | 1.2501 | 1.2501 | 0/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | deep | total_ms | 1 | 1 | 500.0 | 500.0 | 500.0 | 1.25 | 1.25 | 1.25 | 0/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | standard | run_wall_ms | 1 | 1 | -1766.25 | -1766.25 | -1766.25 | 0.4113 | 0.4113 | 0.4113 | 1/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | standard | total_ms | 1 | 1 | -600.0 | -600.0 | -600.0 | 0.4001 | 0.4001 | 0.4001 | 1/0 | 0/0 | 0/0 | 0/0 |

按 `corpus_cell` 分桶(§10.2-3:各主要题型/档位各读自己的配对中位比——全局那一行会把两档相反的偏离抵消掉):

| policy_version | variant | corpus_cell | metric | n_pairs | n_ratio_pairs | Δms p50 | Δms p95 | Δms max | ratio p50 | ratio p95 | ratio max | n_censored(基线/变体) | n_failed(基线/变体) | n_unfinished(基线/变体) | n_unpaired(基线/变体) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v2 | prefix_delta_lean | B_kg | run_wall_ms | 1 | 1 | -1766.25 | -1766.25 | -1766.25 | 0.4113 | 0.4113 | 0.4113 | 1/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | B_kg | total_ms | 1 | 1 | -600.0 | -600.0 | -600.0 | 0.4001 | 0.4001 | 0.4001 | 1/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | B_nokg | run_wall_ms | 1 | 1 | 1250.375 | 1250.375 | 1250.375 | 1.2501 | 1.2501 | 1.2501 | 0/0 | 0/0 | 0/0 | 0/0 |
| v2 | prefix_delta_lean | B_nokg | total_ms | 1 | 1 | 500.0 | 500.0 | 500.0 | 1.25 | 1.25 | 1.25 | 0/0 | 0/0 | 0/0 | 0/0 |
