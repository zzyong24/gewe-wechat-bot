#!/usr/bin/env bash
# 按 systemd EnvironmentFile 的规则读一个 KEY=VALUE 文件，导出后执行后面的命令（不打印任何值）。
# 为什么不直接 source：值里有 & ; $ 空格 等字符时 bash 会解释它们，导致密钥被截断或执行出错。
#   scripts/with_env.sh /etc/gewe-wechat-bot.env .venv/bin/python poller.py
set -euo pipefail
f=${1:?用法: scripts/with_env.sh <env文件> <命令...>}; shift
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  case "$line" in ''|\#*|\;*) continue ;; esac
  k=${line%%=*}; v=${line#*=}
  k=${k//[[:space:]]/}
  [[ $k =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "跳过无效行（键名不合法）" >&2; continue; }
  export "$k=$v"
done < "$f"
exec "$@"
