#!/usr/bin/env bash
# 把本仓库安装成一个 skill：整个目录软链进目标 skills 目录。
#
# 只软链 SKILL.md 是不够的 —— 脚本（check_prd_code.py、prdcode/）也要在同目录，
# 否则 skill 里的命令找不到脚本。所以这里软链的是整个仓库目录。
#
# 用法：
#   ./install.sh                     # 自动挑选已存在的 skills 目录
#   ./install.sh ~/.claude/skills    # 显式指定目标目录
set -euo pipefail

NAME="check-prd-code"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" != "" ]; then
  TARGET_DIR="$1"
else
  TARGET_DIR=""
  for d in "$HOME/.agents/skills" "$HOME/.config/opencode/skills" \
           "$HOME/.claude/skills" "$HOME/.codebuddy/skills"; do
    if [ -d "$d" ]; then
      TARGET_DIR="$d"
      break
    fi
  done
fi

if [ -z "$TARGET_DIR" ]; then
  echo "没找到现成的 skills 目录，请显式指定：$0 <skills目录>" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"
LINK="$TARGET_DIR/$NAME"

if [ -L "$LINK" ] || [ -e "$LINK" ]; then
  echo "目标已存在，未改动：$LINK" >&2
  echo "如需重装，先删除它再重跑本脚本。" >&2
  exit 0
fi

ln -s "$REPO_DIR" "$LINK"
echo "已安装：$LINK -> $REPO_DIR"
