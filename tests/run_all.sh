#!/usr/bin/env bash
# 跑全部单测：每个 tests/test_*.py 单独一个进程。全 mock：不连 gewe、不调模型、不发任何消息。
#   ./tests/run_all.sh            # 用 .venv/bin/python（没有就用 python3）
#   PY=/path/to/python ./tests/run_all.sh
set -u
cd "$(dirname "$0")/.."
PY=${PY:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 )}
# 测试用的假身份 / 假配置（与 tests/ 里的假 id 对应），真密钥一律清掉
export BOT_DATA_DIR=$(mktemp -d)
export BOT_OWNER_WXID=wxid_owner_demo BOT_WXID=wxid_bot_demo \
       BOT_IGNORE_WXIDS=wxid_peerbot_a,wxid_peerbot_b BOT_OBSERVE_BOTS="wxid_peerbot_a:机器人甲,wxid_peerbot_b:机器人乙" \
       BOT_LEARN_STYLE_ONLY_ROOM=20000000003@chatroom BOT_GZH_ID=gh_demo000000 BOT_GZH_NAME=示例公众号 \
       BOT_GZH_X_USER=your_x_handle BOT_AIHOT_API=https://aihot.example.com BOT_TODO_INBOX=$BOT_DATA_DIR/alerts \
       BOT_TTS_USAGE_LOG=$BOT_DATA_DIR/tts_usage.jsonl BOT_RATE_GROUP_GAP_MIN=0 BOT_RATE_GROUP_GAP_MAX=0
unset GEWE_TOKEN MINIMAX_API_KEY AMAP_KEY DOUBAO_TTS_API_KEY BWS_ACCESS_TOKEN AIHOT_BASIC_AUTH_PASSWORD BOT_SEND_ENABLED BOT_SEND_ALLOW
LOGDIR=${LOGDIR:-$(mktemp -d)}; mkdir -p "$LOGDIR"; pass=0; fail=0; failed=""
for t in tests/test_*.py; do
  n=$(basename "$t" .py)
  if timeout 300 "$PY" "$t" > "$LOGDIR/$n.log" 2>&1; then pass=$((pass+1)); else fail=$((fail+1)); failed="$failed $n"; fi
done
echo "通过 $pass 个，失败 $fail 个${failed:+：$failed}（日志在 $LOGDIR）"
[ "$fail" -eq 0 ]
