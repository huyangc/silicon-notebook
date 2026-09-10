# reflect T0 轨迹聚合

- 输入行数:10
- 分组维度:`consumer,mode,effort,kg_in_scope,policy_version,optimization`
- 分位数门槛:`min_samples=5`

## 分组概览

| consumer | mode | effort | kg_in_scope | policy_version | optimization | n_runs | reflect_turns(n/均值) | total_ms(P50/P95) | stale_breaker | trace_truncated |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ask_single | reasoning | standard | True | legacy | unknown | 3 | 3/2.0 | n=3(unknown) | 0/3 | 0/3 |
| ask_single | reasoning | standard | True | v2 | off | 6 | 6/3.667 | 1003.0/1017.0 | 0/6 | 0/6 |
| ask_single | reasoning | standard | True | v2 | prefix_snapshot | 1 | 1/3.0 | n=1(unknown) | 0/1 | 0/1 |

### ask_single / reasoning / standard / True / legacy / unknown

- n_runs: 3
- termination_reason: model_end=3
- termination_inferred: True=3
- status: done=3
- actions_by_type: ppr=3
- skip_reasons: kg_unavailable=3

引用贡献(§4.4):

| action | steps | steps_with_ids | unknown_steps | cited_hits | runs_observed | runs_unknown |
| --- | --- | --- | --- | --- | --- | --- |
| ppr | 3 | 3 | 0 | 3 | 3 | 0 |

### ask_single / reasoning / standard / True / v2 / off

- n_runs: 6
- termination_reason: model_sufficient=6
- termination_inferred: False=6
- status: done=6
- optimization: off=6
- actions_by_type: ppr=6
- skip_reasons: kg_unavailable=6
- context_chars: bytes_total=57600

引用贡献(§4.4):

| action | steps | steps_with_ids | unknown_steps | cited_hits | runs_observed | runs_unknown |
| --- | --- | --- | --- | --- | --- | --- |
| ppr | 6 | 6 | 0 | 12 | 6 | 0 |

### ask_single / reasoning / standard / True / v2 / prefix_snapshot

- n_runs: 1
- termination_reason: model_sufficient=1
- termination_inferred: False=1
- status: done=1
- optimization: prefix_snapshot=1
- actions_by_type: ppr=1
- skip_reasons: kg_unavailable=1
- context_chars: bytes_total=8800

引用贡献(§4.4):

| action | steps | steps_with_ids | unknown_steps | cited_hits | runs_observed | runs_unknown |
| --- | --- | --- | --- | --- | --- | --- |
| ppr | 1 | 1 | 0 | 2 | 1 | 0 |

## legacy / v2 对照(成对)

| question_key | corpus_cell | effort | consumer | mode | trace_source | has_intent_contract | v2 optimization | legacy n | v2 n | legacy reflect_turns | v2 reflect_turns | legacy total_ms | v2 total_ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B-q01 | B_kg | standard | ask_single | reasoning | unknown | unknown | off | 3 | 6 | 2.0 | 3.667 | 1001.333 | 1007.167 |
| B-q01 | B_kg | standard | ask_single | reasoning | unknown | unknown | prefix_snapshot | 3 | 1 | 2.0 | 3.0 | 1001.333 | 900.0 |

## off / 优化变体对照(成对,同 policy_version)

| question_key | corpus_cell | effort | consumer | mode | trace_source | has_intent_contract | policy_version | variant | off n | variant n | off run_wall_ms(n) | variant run_wall_ms(n) | off model_calls_real(n) | variant model_calls_real(n) | off context_rebuilds(n) | variant context_rebuilds(n) | off assessment_rows_total(n) | variant assessment_rows_total(n) | variant prefix_bytes_median(n) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B-q01 | B_kg | standard | ask_single | reasoning | unknown | unknown | v2 | prefix_snapshot | 6 | 1 | 9007.167(n=6) | 6000.0(n=1) | 4.0(n=6) | 3.0(n=1) | unknown(n=0) | unknown(n=0) | unknown(n=0) | unknown(n=0) | 700.0(n=1) |
