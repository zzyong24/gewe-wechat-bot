# gewe-wechat-bot：基于 GeWe 的微信群 AI 小助手

一个跑在你自己机器上的微信群机器人：接 GeWe 的回调收消息，按群开关分发给各个功能（成员列表、发言统计、AI 陪聊、地图、小游戏……），回复再通过 GeWe 发回群里。默认**只演练不发送**，要你亲手打开发送闸、把群加进白名单才会说话。

> 机器人在群里显示的名字默认叫「小助手」，代码和测试里多处按这个名字识别 @。改成你的机器人昵称：`python scripts/rename_bot.py 你的昵称 --apply`（先不加 `--apply` 预览）。

---

## 0. 它能做什么

- 被 @ 时回答：群成员列表、今日发言统计、AI 陪聊（接大模型，可选联网搜索、识图、语音）、天气 / 附近 / 路线（高德）。
- 群里的自动行为：进群欢迎、退群送别、每天的 AI 日报 / AI 快讯、偶尔主动插话。**这些默认都是关的**。
- 小游戏和签到：猜成语、扫雷、成语接龙、谁是卧底、猜歌、五子棋、签到积分等。
- 管理：群主 / 管理员在群里 @ 它就能开关功能；掉线时只给你发告警，不会自己重新登录。

**先读这三句：**

1. **GeWe 不是微信官方接口**。它用的是 iPad / Mac 协议，这类用法**违反《微信软件许可及服务协议》**，账号**有被限制甚至封号的风险**，后果由你自己承担。
2. **只用小号，只在你自己建的、或者群主同意的群里用。**
3. **不加人、不群发、不做营销。** 这份代码里没有加好友、拉群、群发的功能，也请不要加上去。

---

## 1. 先选路

| | 微信官方 ClawBot 插件 | 企业微信（官方 API / 群机器人） | GeWe（本项目） |
|---|---|---|---|
| 是否官方 | ✅ 官方 | ✅ 官方 | ❌ 第三方协议 |
| 能进普通微信群 | ❌ 只能单聊，不能拉进群 | ⚠️ 只在企业微信的群里；普通微信群不行 | ✅ |
| 封号风险 | 无 | 无 | **有** |
| 费用 | 免费（要自己的 OpenClaw 和模型额度） | 企业认证 / 接口额度 | GeWe 按节点收费（注册送 7 天试用） |
| 适合 | 你自己在微信里和 AI 一对一聊 | 公司内部群、客户服务 | 你自己的普通微信群里放一个 AI 群友 |

**能用官方方案就用官方方案。** 只有「必须进普通微信群」这个需求，才值得接受 GeWe 的风险。

- ClawBot：微信「我 → 设置 → 插件」里看安装指令（逐步放量中），文档见 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)。
- 企业微信：[开发者中心](https://developer.work.weixin.qq.com/)。

---

## 2. 准备清单

- [ ] **一个实名的老微信小号**：注册久、实名、平时正常用过。新号、未实名号、被封过的号，GeWe 自己都写了不建议接入。
- [ ] **一台有公网地址的机器**：GeWe 要把消息 POST 到一个公网 URL（内网穿透只适合临时调试）。一台最便宜的云服务器就够，机器人就跑在它上面。
- [ ] **Python ≥ 3.10**。
- [ ] **GeWe 账号**：在 [manager.geweapi.com](https://manager.geweapi.com) 注册，默认开通 7 天试用并生成一个 Token。
- [ ] （可选）**大模型 Key**：陪聊、识图、语音、生图、联网搜索用的是 MiniMax。不填的话，规则类功能照常能用。
- [ ] （可选）高德 Web 服务 Key（天气 / 附近 / 路线）。

---

## 3. 接入 GeWe

### 3.1 拿 Token

登录 GeWe 控制台 →「令牌管理」→ 复制 `TokenId`。**它只放进环境变量**（第 4 步的 `/etc/gewe-wechat-bot.env`），不要写进代码、bot.env 或 git。

### 3.2 扫码登录「执行节点」

GeWe 把一个登录着的微信叫做「执行节点」，用 `appId` 标识。

```bash
export GEWE_TOKEN=...          # 只在当前终端里设置，别写进 shell 历史
python scripts/login_qr.py     # 取二维码 → data/login_qr.png（约 150 秒内有效）
# 用小号手机微信扫码并确认
python scripts/login_check.py  # 轮询登录结果，成功后把 appId 写进 data/gewe_appid.txt
```

> ⚠️ **appId 的坑**：第一次登录 appId 传空，GeWe 会分配一个新的。**以后每次重新登录都必须带上这个 appId**。不带的话等于新开一台设备，微信「登录过的设备」里会堆出好几个 iPad，越登越容易掉线、被封。脚本会自动读写 `data/gewe_appid.txt`。如果 GeWe 那边真的给了新 appId，脚本会写回文件，poller 下一次请求就会用新值。
>
> 新设备登录后的头 24 小时掉线一次是正常的，用原 appId 重新扫一次就好。

### 3.3 发第一条消息（自检）

登录成功后，先给「文件传输助手」`filehelper` 发一句，确认发送通路是通的：

```bash
python -c "import gewe_client as g; print(g.post_text('filehelper', 'hello from bot'))"
```

### 3.4 配置回调

GeWe 收到消息后会 **POST JSON** 到你的回调地址。要求：

- **公网可以访问**；
- **3 秒内返回**，否则这次回调就丢了（所以本项目是先落盘、立刻返回 200，再在后台处理）；
- 地址里带一段随机 secret（GeWe 推送不能自定义请求头），比如 `https://你的域名/callback/<一长串随机字符>`。

在 GeWe 控制台「访问控制」里填上回调地址（第 4.2 步启动回调服务后再填）。设好之后 GeWe 会先发一条「验证回调地址是否可用」的测试包，程序会识别并跳过它。

注意：**通过接口发出去的消息不会触发回调**，手机上发的才会。

---

## 4. 跑起来

```bash
git clone <本仓库> /opt/gewe-wechat-bot && cd /opt/gewe-wechat-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r relay/requirements.txt
cp bot.env.example bot.env                     # 非密钥配置（发送闸、白名单、身份）
sudo install -m 600 .env.example /etc/gewe-wechat-bot.env && sudo nano /etc/gewe-wechat-bot.env   # 密钥
```

### 4.1 先用模拟消息测（不连 GeWe）

```bash
.venv/bin/python simulate.py            # 本地模拟群消息走一遍路由
tests/run_all.sh                        # 全部单测（不联网、不需要任何 Key）
.venv/bin/python scripts/smoke_test.py  # 端到端：本地起中转 + poller + 回调服务，模拟消息全程走通，不发任何微信
```

### 4.2 启动回调服务（同一台公网机器上跑两个进程）

- `relay/relay.py`：对外收 GeWe 回调，存进本地 SQLite 队列后立刻返回（满足 3 秒要求），顺便当临时图床，因为 GeWe 发图要一个公网 URL。
- `poller.py`：从 `127.0.0.1` 上的 relay 长轮询拉消息，完成去重、合并连发、按群排队、路由、发送，还负责掉线告警和定时任务。

```bash
SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
cat > .relay_config.json <<JSON
{"server_url": "http://127.0.0.1:8787", "secret": "$SECRET", "last_pulled_id": 0, "public_img_base": "http://<公网地址>:8787"}
JSON
chmod 600 .relay_config.json

# 终端 1：中转（对外 8787）
RELAY_SECRET=$SECRET RELAY_DB=data/queue.db RELAY_IMG_DIR=data/img .venv/bin/python relay/relay.py
# 终端 2：机器人
scripts/with_env.sh /etc/gewe-wechat-bot.env .venv/bin/python poller.py      # 日志在 data/poller.log
```

GeWe 回调地址填 `http://<公网地址>:8787/callback/<SECRET>`，记得在安全组 / 防火墙放行 8787。也可以 `scripts/with_env.sh /etc/gewe-wechat-bot.env .venv/bin/python scripts/login_check.py --set-callback` 一键设置，但它会用 `.relay_config.json` 里的 `server_url`，所以只适合双机部署。单机时 `server_url` 是 127.0.0.1，请在 GeWe 控制台手动填公网地址。

> `callback_server.py` 是只用来调试的单进程版：收到就处理，没有去重、排队、掉线告警和定时任务。只在本地调试时用（`BOT_OFFLINE=1` 时用假群成员，不调 GeWe）。

先在前台跑着看日志；要长期挂着、开机自启，见文末「附录：部署到服务器长期运行」。

### 4.3 默认是 dry-run

`bot.env` 里 `BOT_SEND_ENABLED=0`：所有回复只写进 outbox 和日志，**一条都不会真发**。先这样跑一阵，看日志里它「想说什么」。

### 4.4 只对白名单真发

```ini
BOT_SEND_ENABLED=1
BOT_SEND_ALLOW=12345678901@chatroom      # 先只放一个你自己的测试群；空 = 一律不发
```

群 id 在日志的「收到 … 群=xxx@chatroom」里能看到。

### 4.5 配模型，打开陪聊

在 `/etc/gewe-wechat-bot.env` 里填好 `MINIMAX_API_KEY`，重启后在群里 `@小助手 开启 陪聊`。人设写在一个文本文件里，用 `BOT_PERSONA_FILE` 指向它；不设就用 `features/chat_companion.py` 里的示例人设。

---

## 5. 三道闸与功能开关

一条回复要真正发出去，必须同时过三道闸：

1. **发送总闸**：`BOT_SEND_ENABLED=1`，否则只演练。
2. **白名单**：目标群或人在 `BOT_SEND_ALLOW` 里（默认是空的）。`data/observe.json` 里标成观察的群会被硬拦，即使误加进白名单也不发。
3. **功能开关**：这个群开了这个功能。生效顺序是群级 > 全局 > 默认，存在 `data/settings.json`。

另外还有一层**全局发送限速**（见第 6 节）。

**群指令**（都要 @机器人；改开关只有群主、群管理员或 `BOT_ADMIN_WXIDS` 能做）：

```
@小助手 功能状态        @小助手 功能帮助
@小助手 开启 陪聊       @小助手 关闭 陪聊       @小助手 恢复默认 陪聊
```

**功能清单与默认开关**

| 默认 | 功能 |
|---|---|
| 开（只在有人触发时回复） | 成员列表、发言统计、进退群提醒、签到、猜成语、扫雷、成语接龙 |
| 关（要调模型 / 花额度 / 会主动说话） | 智能陪聊、AI 日报、AI 快讯、进群欢迎、进群语音、退群送别、主动插话、插话判断、长期记忆、联网搜索、画图、语音转文字、识图、语音回复、地图查询、位置回复、公众号新文推群 |
| 关（娱乐） | 结婚、师徒、装备、买卖群友、谁是卧底、猜歌、五子棋 |

只读类的东西（掉线告警、每日学习）不往群里发消息。

---

## 6. 风险与降低风险

- **原文先读**：[《腾讯微信软件许可及服务协议》](https://weixin.qq.com/agreement/service_agreement)、[《微信个人账号使用规范》](https://weixin.qq.com/agreement/personal_account?lang=zh_CN)、GeWe [风险说明](https://doc.geweapi.com/doc-6356339)、[调试指南](https://doc.geweapi.com/doc-3146245)。
- **GeWe 列出的高风险行为**：
  - 异地添加好友、高频群发、自动通过好友、内容违规；
  - 短时间给很多人发消息：1 分钟超过 10–15 个好友就有风险；
  - 频繁换设备 / 换号、每次掉线都生成新二维码；
  - 新号、未实名号，或新号刚登录就猛发消息。
- **限速**：所有发送都经过 `send_limiter.py`，可以用环境变量调整：
  - 全局每 60 秒最多 `BOT_RATE_PER_MIN`（默认 40）条；
  - 同一个群连续两条之间随机等 `BOT_RATE_GROUP_GAP_MIN`–`BOT_RATE_GROUP_GAP_MAX` 秒（默认 2–5 秒）；
  - 超过 `BOT_RATE_MAX_WAIT` 秒还排不上，这条就丢弃并记日志；
  - 设成 0 表示关闭（不推荐）。
- **掉线只告警**：`features/online_watch.py` 每 5 分钟查一次在线状态。离线、查询失败、白天长时间没有回调时，只写日志和 `data/alerts/`。**程序里没有任何自动重登**，要人来跑 `scripts/login_qr.py`。
- **Token 只在环境变量里**：日志 handler 会把 Token / Key 抹成 `***`，脚本也不会打印 Token。
- **隐私**：`data/` 里有消息原文、群成员昵称、上下文和长期记忆，已被 `.gitignore` 排除；不要提交，也不要随手分享日志。要在群里说明有机器人在，记忆功能可以用「@小助手 忘掉我」清除。

---

## 7. 常见问题

**收不到回调**

- 先 `curl -X POST <回调地址> -H 'Content-Type: application/json' -d '{}'`，确认公网能访问（防火墙、安全组、端口都开了）；
- 确认回调地址里的 secret 填对了（错的返回 404）；
- 用 `scripts/login_check.py` 确认账号在线；
- 注意接口发出的消息本来就没有回调。

**发送失败**

- 看日志里 gewe 返回的 `ret` / `msg`；
- `data/gewe_appid.txt` 里是不是当前 appId（重新登录后可能变了）；
- 目标在不在白名单里，`BOT_SEND_ENABLED` 是不是 1；
- 新设备刚登录有一段时间会被限制部分操作。

**掉线**

- 用**原 appId** 重新扫码，不要每次都新建；
- 头 24 小时掉一次是正常的；
- 频繁掉线的话，先降频，检查是不是异地登录。

**群里部分人看不到它的消息 / 回复变慢**

- 高频、同质化的内容会被微信降权，把限速调慢一点，少发长串列表；
- 也可能是对方屏蔽了这个群，或者微信版本问题。

**`source /etc/gewe-wechat-bot.env` 之后 Key 不对**

这个文件是 systemd 格式，不能 source。手动跑用 `scripts/with_env.sh /etc/gewe-wechat-bot.env <命令>`。

---

## 8. 许可证与免责声明

许可证**待定**（仓库里的 LICENSE 目前是 MIT 草稿，以作者最终确定的为准）。

本项目只用于学习和个人群聊自用。GeWe 是第三方服务，与腾讯无关；用它登录微信违反微信用户协议，可能导致账号被限制或封禁。对因使用本项目造成的任何账号、数据或法律后果，作者不承担责任。请不要用于营销、群发、骚扰、爬取他人隐私，或任何违法违规用途。

部分功能参考了这些开源项目（只参考思路或移植了 MIT 代码，详见对应文件头注释）：
- nonebot-plugin-handle / nonebot-plugin-minesweeper（MIT）
- SXP-Simon/astrbot_plugin_qq_group_daily_analysis（MIT）
- groupmate_waifu（MIT）、nonebot_plugin_xiuxian（MIT）
- XYBotV2（GPL，只参考逻辑，没有复制代码）

---

## 附录：部署到服务器长期运行

> 正文只用到「GeWe + 一台公网机器 + 前台跑 relay 和 poller」。这一章是长期运行时的做法，按需取用。

### 附录 A systemd 常驻（单机：relay + poller 同机）

```bash
sudo useradd -r -s /usr/sbin/nologin gewebot
sudo chown -R gewebot: /opt/gewe-wechat-bot
echo "RELAY_SECRET=<和 .relay_config.json 里同一个 secret>" | sudo tee /etc/gewe-relay.env >/dev/null && sudo chmod 600 /etc/gewe-relay.env
sudo cp deploy/gewe-relay.service deploy/gewe-wechat-bot-poller.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gewe-relay gewe-wechat-bot-poller
journalctl -u gewe-relay -f              # 机器人日志看 data/poller.log
```

### 附录 B 双机：公网中转 + 别处跑机器人

适合机器人想放在家里或内网机器上、不想暴露端口的情况：

- **中转机**（公网）：同样 clone 本仓库，只需要 `pip install -r relay/requirements.txt`，用 `deploy/gewe-relay.service` 常驻。
- **机器人机**：`.relay_config.json` 的 `server_url` 改成中转机的公网地址（建议套 HTTPS），用 `deploy/gewe-wechat-bot-poller.service` 常驻。这时 `scripts/login_check.py --set-callback` 可以一键设回调。
- 跨网络时会有连接超时、断线和退避，poller 已经内置处理。

### 附录 C 部署的坑

- **别用 root 跑**：root 写出来的 data/ 文件以后换普通用户就读写不了。已经用 root 跑过的话，先 `chown -R gewebot: /opt/gewe-wechat-bot/data`。
- **中文字体**：服务器没有中文字体时，日报、扫雷之类的图片里中文会显示成方块。装 `apt install fonts-noto-cjk`，代码里写死的路径 `/usr/share/fonts/opentype/noto/` 就是这个包的安装位置。
- **env 文件不能 source**：见第 7 章常见问题。
