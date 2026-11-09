#!/usr/bin/env bash
# 一键推送 GitHub。
# 背景：本机 git-over-HTTPS 访问 GitHub 不通，origin 已改为 SSH：
#       git@github.com:Yzm889/-Smart-video-highlights.git
# 用法：
#   ./push.sh              # 推 main 分支
#   ./push.sh --tags       # 推全部标签（v* 标签触发 release 构建）
#   ./push.sh main --tags  # 推 main + 标签
#   ./push.sh <任意 git push origin 参数...>
set -euo pipefail
cd "$(dirname "$0")"

remote_url=$(git remote get-url origin)
case "$remote_url" in
  git@*|ssh://*) ;;
  *)
    echo "⚠️ origin 不是 SSH（当前：$remote_url）" >&2
    echo "   修复：git remote set-url origin git@github.com:Yzm889/-Smart-video-highlights.git" >&2
    exit 1
    ;;
esac

if [ $# -eq 0 ]; then
  git push origin main
else
  git push origin "$@"
fi
