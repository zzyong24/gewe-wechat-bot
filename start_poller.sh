#!/usr/bin/env bash
# 开发机上手动启动 / 重启 poller（生产建议用 deploy/gewe-wechat-bot-poller.service）。
# 密钥需已在环境变量里（不要 source .env：值里有特殊字符会出错，见 README 常见问题）。非密钥配置在 bot.env（poller 自己加载）；密钥只检查是否存在，不打印。
set -u
cd "$(dirname "$0")"
for k in GEWE_TOKEN; do [ -n "${!k:-}" ] || { echo "缺少环境变量 $k，不启动"; exit 1; }; done
for k in MINIMAX_API_KEY AMAP_KEY DOUBAO_TTS_API_KEY; do [ -n "${!k:-}" ] || echo "提醒：$k 未设置（相关功能降级）"; done
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
OLD=$(pgrep -f "\.venv/bin/python poller.py")
if [ -n "$OLD" ]; then kill -TERM $OLD; for i in $(seq 1 60); do kill -0 $OLD 2>/dev/null || break; sleep 1; done; fi
nohup .venv/bin/python poller.py >> data/poller.stdout 2>&1 &
sleep 3
P=$(pgrep -f "\.venv/bin/python poller.py"); echo "$P" > poller.pid; echo "poller pid=$P"
