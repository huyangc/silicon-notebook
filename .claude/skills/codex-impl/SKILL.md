---
name: codex-impl
description: Claude 设计、Codex 实现的工作流。规格已定死的实现任务交给本机 codex CLI（codex exec）执行，Claude 负责写规格、评审 diff、返工与收尾提交。用户说「交给 codex 实现」「用 codex 写」或调用 /codex-impl 时使用；需要设计取舍的任务先由 Claude 定稿再用。
---

# codex-impl：Claude 设计，Codex 实现

分工：**Claude** 负责方案、规格、评审、返工判断、门禁、提交与 PR；**Codex** 只按规格改
worktree 里的代码并跑测试。Codex 不提交，脚本也会检查 HEAD 有没有动。

脚本：`.claude/skills/codex-impl/codex-impl.sh`（下面简写为 `$S`，调用时一律写绝对路径）。
运行记录在 `~/.claude/codex-impl/<时间>-<标签>/`。

## 1. 先把方案定稿

- 设计取舍、架构判断由 Claude 自己做完（必要时用 opus 的 Plan 子代理或 spec 评审）。
  **交给 codex 的必须是已经定死的规格**；带着未决问题交出去，返工代价最高。
- 需要用户拍板的点先问，不要写进规格让 codex 猜。
- worktree 先保持干净：已有未提交改动就先做一个 WIP commit，这样 codex 的 diff 能单独看。

## 2. 写规格文件

写到 scratchpad（不进仓库），按下面的结构：

```markdown
## 目标
一两句：这次改动之后什么成立。

## 改动清单
- `path/to/file.py`：改什么、怎么改（函数/类名、签名、行为）。
- ...

## 权威约束
相关的 AGENTS.md / docs 章节链接与必须遵守的不变量（只列和本任务有关的）。

## 不要做
明确排除的范围、不要碰的文件。

## 验收
- 要跑的测试命令（精确到文件或 -k），以及预期结果。
- 需要新增的测试与它们断言的行为。
```

规格要自足：codex 看不到这段对话，只能看到规格、仓库和 `AGENTS.md`。执行约定
（不提交、不 npm install、重负载走 heavy-gate、报告格式）由 `preamble.md` 自动加在前面，规格里不用重复。

## 3. 后台运行

```bash
bash $S run <worktree 绝对路径> <spec.md 绝对路径> <标签>
```

- 用 Bash 的 `run_in_background: true` 运行，跑完会自动通知你；**不要轮询**。
- 输出最后两行是 `RUN_DIR=...` 和 `EXIT=...`。退出码：0 完成；1 codex 失败（报错事件、
  空报告、CLI 版本过旧等，stderr 会说明）；3 codex 擅自提交了（HEAD 变了）。
- `EXIT=0` 只说明 codex 跑完并交了报告，**不代表验收通过**；codex 会在报告里如实写失败的测试，必须读报告。
- 验收命令写全解释器路径（与 `AGENTS.md` 的验证命令一致），codex 的 shell 里裸 `python3` 可能没有 pytest。
- 退出码 1 且提示 CLI 过旧：告诉用户升级 codex，不要换模型绕过去。

## 4. 核对，不轻信报告

1. `bash $S show <RUN_DIR>` 读 codex 的最终报告。
2. 自己看 `git -C <worktree> status` 与 `git -C <worktree> diff`：报告里说的改动和实际 diff
   要对得上；有没有越界改动、删断言、跳过测试。
3. 「验证」里的关键命令自己重跑一遍，**直接读退出码**（重负载命令经 heavy-gate）。
4. 改动涉及授权、schema/迁移、并发/共享状态、跨模块契约时，按
   [Delegation and review](../../../docs/development.md#delegation-and-review) 派
   `spec-review` 和 `code-quality-review` 两个角色独立评审（把规格文件路径和 diff 范围交给它们）。

## 5. 返工：同一个会话继续

评审发现的问题写成追问文件（列出问题、期望的修法、要重跑的测试），然后：

```bash
bash $S resume <RUN_DIR> <followup.md 绝对路径>
```

codex 在原会话里接着做，上下文还在。上一轮的记录会改名为 `round<N>-*` 保留。
**同一个问题返工两次还不对**，先怀疑规格本身：Claude 修正规格或自己动手，不要第三次原样丢回去。
codex 报告里「待 Claude 决定」的条目，由 Claude 判断或去问用户，不要原样转回给 codex。

## 6. 收尾

Claude 自己跑门禁、提交（提交信息照常写）、按用户授权开 PR。codex 只是执行者；
PR 说明里写清楚哪些部分由 codex 实现、Claude 评审了什么。

## 什么时候不要用

- 一两处小改动：Claude 直接改比写规格更快。
- 还需要探索或设计的任务。
- 需要浏览器预览、真机验证、生产诊断的工作：codex 沙箱里做不了，留给 Claude。
