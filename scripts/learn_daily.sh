#!/usr/bin/env bash
# 小助手每日学习（定时 23:30）。需要环境变量 MINIMAX_API_KEY；不发微信消息。
cd "$(dirname "$0")/.." && exec .venv/bin/python -m features.learn learn "$@"
