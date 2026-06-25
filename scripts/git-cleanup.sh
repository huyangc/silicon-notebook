#!/usr/bin/env bash
# silicon-notebook 分支/worktree 清理脚本 —— 删除「PR 已合并」的本地分支与 worktree。
#
#   scripts/git-cleanup.sh            预演(dry-run):只列出将删除的,不动任何东西
#   scripts/git-cleanup.sh --apply    实际删除本地已合并分支 + worktree
#   scripts/git-cleanup.sh --apply --remote   再删 GitHub 上对应的已合并远程分支
#
# 为什么需要它:每个 Claude Code / codex 子代理会话都会自动建一个 claude/<名>-<hash>
# 或 codex/<feature> 分支 + 专属 worktree(.claude/worktrees/ 下)。PR 合并后这些不会
# 自动消失,日积月累就泛滥。本脚本按 GitHub PR 的 MERGED 状态(而非 git branch --merged,
# 因为本仓库走 rebase merge、patch-id 会失配)判定哪些已完成,安全清理。
#
# 永远保护:master、当前所在分支、当前 worktree、detached 的 worktree(如 eval)、
#           backup/* 备份分支。只删 PR 状态为 MERGED 的,绝不碰未提交改动的 worktree。
#
# 依赖:gh(需已登录)、git。默认 dry-run,确认无误再加 --apply。
set -euo pipefail

apply=0 remote=0
for a in "$@"; do
  case "$a" in
    --apply)   apply=1 ;;
    --remote)  remote=1 ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "未知参数:$a(用 -h 看用法)" >&2; exit 2 ;;
  esac
done

# 主仓库根(从任意 worktree 跑都取主仓库;worktree list 第一行恒为主仓库)
main=$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')
cur_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
cur_wt=$(git rev-parse --show-toplevel)

protected() {  # 受保护分支返回 0(不删)
  local b=$1
  [ "$b" = master ] && return 0
  [ "$b" = "$cur_branch" ] && return 0
  case "$b" in backup/*) return 0 ;; esac
  return 1
}

echo "▶ git fetch --prune …"
git -C "$main" fetch origin --prune -q

echo "▶ 拉取已合并 PR 列表 …"
merged=$(gh pr list --state merged --limit 1000 --json headRefName -q '.[].headRefName' | sort -u)
is_merged() { printf '%s\n' "$merged" | grep -qxF "$1"; }

echo
echo "== Worktree(已合并)=="
removed_wt=0
while IFS=$'\t' read -r wt br; do
  [ "$wt" = "$main" ] && continue
  [ "$wt" = "$cur_wt" ] && continue
  [ "$br" = "-" ] && { echo "  · 跳过(detached):$wt"; continue; }
  is_merged "$br" || continue
  if [ "$apply" = 1 ]; then
    if git -C "$main" worktree remove "$wt" 2>/dev/null; then
      echo "  ✓ 删除 worktree:$wt（$br）"; removed_wt=$((removed_wt+1))
    else
      echo "  ⚠ 跳过(可能有未提交改动):$wt"
    fi
  else
    echo "  [dry] 将删 worktree:$wt（$br）"
  fi
done < <(git -C "$main" worktree list --porcelain | awk '
  /^worktree /{wt=$2}
  /^branch /{br=$2; sub("refs/heads/","",br); print wt"\t"br; next}
  /^detached/{print wt"\t-"; next}')

[ "$apply" = 1 ] && git -C "$main" worktree prune

echo
echo "== 本地分支(已合并)=="
deleted_br=0
while read -r b; do
  protected "$b" && continue
  is_merged "$b" || continue
  if [ "$apply" = 1 ]; then
    if git -C "$main" branch -D "$b" >/dev/null 2>&1; then
      echo "  ✓ 删除分支:$b"; deleted_br=$((deleted_br+1))
    else
      echo "  ⚠ 删分支失败:$b"
    fi
  else
    echo "  [dry] 将删分支:$b"
  fi
done < <(git -C "$main" for-each-ref --format='%(refname:short)' refs/heads/)

if [ "$remote" = 1 ]; then
  echo
  echo "== 远程分支(已合并)=="
  tmp=$(mktemp)
  git -C "$main" for-each-ref --format='%(refname:short)' refs/remotes/origin/ \
    | sed 's#^origin/##' | grep -vxE 'master|HEAD' | while read -r b; do
        is_merged "$b" || continue
        if [ "$apply" = 1 ]; then echo "$b"; else echo "  [dry] 将删远程:origin/$b"; fi
      done > "$tmp"
  if [ "$apply" = 1 ] && [ -s "$tmp" ]; then
    xargs git -C "$main" push origin --delete < "$tmp"
  fi
  rm -f "$tmp"
fi

echo
if [ "$apply" = 1 ]; then
  echo "✅ 完成:删除 worktree ${removed_wt} 个、本地分支 ${deleted_br} 个$([ "$remote" = 1 ] && echo '、并清理远程已合并分支')。"
else
  echo "ℹ️ 以上为预演。确认无误后加 --apply 执行;--remote 可一并清远程(GitHub 已开合并自动删,平时不需要)。"
fi
