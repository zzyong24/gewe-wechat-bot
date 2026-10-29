"""GeWe API 轻量封装。

- token 只从环境变量 GEWE_TOKEN 读取，绝不打印/落盘。
- appId 从 data/gewe_appid.txt（可用环境变量 GEWE_APPID_FILE 覆盖）读取。
- 所有接口：POST + JSON，成功时 ret == 200。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import time

import logging

import requests

log = logging.getLogger("gewe")

BASE_URL = os.environ.get("GEWE_BASE_URL", "https://api.geweapi.com")
APPID_FILE = Path(os.environ.get("GEWE_APPID_FILE", str(Path(__file__).resolve().parent / "data" / "gewe_appid.txt")))
DEFAULT_TIMEOUT = 20
_session = requests.Session()     # keep-alive：实测新建连接 ~1.8–3s，复用连接 ~0.25–1s


class GeweError(RuntimeError):
    def __init__(self, path: str, ret: Any, msg: str):
        super().__init__(f"gewe {path} failed: ret={ret} msg={msg}")
        self.path, self.ret, self.msg = path, ret, msg


def _token() -> str:
    tok = os.environ.get("GEWE_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("环境变量 GEWE_TOKEN 未设置")
    return tok


def get_app_id() -> str:
    app_id = APPID_FILE.read_text(encoding="utf-8").strip()
    if not app_id:
        raise RuntimeError(f"{APPID_FILE} 为空")
    return app_id


CONNECT_TIMEOUT = float(os.environ.get("BOT_CONNECT_TIMEOUT", "5"))
_SEND_HINT = ("/message/post", "/message/forward", "/sns/send", "/message/revoke")


def _is_send(path: str) -> bool:
    return any(h in path for h in _SEND_HINT)


def _never_sent(e: Exception) -> bool:
    """连接阶段就失败（TCP 没建起来：拒绝连接/DNS/网络不可达/建连超时）→ 请求肯定没发出，重试不会重复发。
    requests 把 urllib3 NewConnectionError 包成 ConnectionError(MaxRetryError(reason=NewConnectionError))。"""
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(e, requests.ConnectionError) or isinstance(e, requests.exceptions.ReadTimeout):
        return False
    try:
        from urllib3.exceptions import NewConnectionError, NameResolutionError
    except ImportError:  # 旧 urllib3 没有 NameResolutionError
        from urllib3.exceptions import NewConnectionError
        NameResolutionError = NewConnectionError
    seen, x = 0, e
    while x is not None and seen < 5:
        if isinstance(x, (NewConnectionError, NameResolutionError)):
            return True
        nxt = getattr(x, "reason", None)
        if nxt is None and x.args and isinstance(x.args[0], Exception):
            nxt = x.args[0]
        x, seen = nxt, seen + 1
    return False


def _retry_safe(path: str, e: Exception) -> bool:
    """发送类：只在请求确定没发出时重试（ReadTimeout / 连接中途断开可能已发出 → 不重试，防重复发）；
    查询类：连接错误都可重试（读超时除外）。"""
    if _is_send(path):
        return _never_sent(e)
    return isinstance(e, requests.exceptions.ConnectTimeout) or (
        isinstance(e, requests.ConnectionError) and not isinstance(e, requests.exceptions.ReadTimeout))


def call(path: str, body: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """调用 gewe 接口，返回 data 字段；ret != 200 抛 GeweError。
    跨境丢包：连接超时 BOT_CONNECT_TIMEOUT（5s）；连接阶段失败（请求还没发出去）最多重试 2 次（0.5s/1s 退避），
    发送类接口只在 ConnectTimeout 时重试（读超时可能已发出 → 不重试，防重复发）；查询类连接错误都重试。"""
    global _session
    payload = {"appId": get_app_id(), **(body or {})}
    headers = {"X-GEWE-TOKEN": _token(), "Content-Type": "application/json"}
    if _is_send(path):                       # 全局发送限速（send_limiter.py；env BOT_RATE_*）
        import send_limiter
        try:
            waited = send_limiter.default().acquire(str((body or {}).get("toWxid", "")))
        except send_limiter.RateLimitTimeout as e:
            raise GeweError(path, None, str(e)) from None
        if waited > 0.5:
            log.info("发送限速：等了 %.1f 秒", waited)
    for attempt in range(3):
        try:
            r = _session.post(BASE_URL + path, json=payload, headers=headers, timeout=(CONNECT_TIMEOUT, timeout))
            break
        except requests.RequestException as e:  # 不把 headers 带进异常信息
            safe = _retry_safe(path, e)
            if attempt < 2 and safe:
                log.warning("gewe %s %s（%s），第 %d 次重试", path, type(e).__name__,
                            "请求未发出" if _is_send(path) else "查询类", attempt + 1)
                _session = requests.Session()
                time.sleep(0.5 * (2 ** attempt))
                continue
            if _is_send(path) and not safe:
                log.warning("gewe %s %s：请求可能已到达服务端，为防重复发送不重试（这条可能已发出也可能丢了）",
                            path, type(e).__name__)
            raise GeweError(path, None, f"network error: {type(e).__name__}") from None
    try:
        resp = r.json()
    except ValueError:
        raise GeweError(path, r.status_code, f"non-json response (http {r.status_code})")
    if resp.get("ret") != 200:
        raise GeweError(path, resp.get("ret"), str(resp.get("msg")))
    return resp.get("data")


# ---- 登录 / 状态 ----
def check_online() -> bool:
    return bool(call("/gewe/v2/api/login/checkOnline"))


# ---- 消息 ----
def post_text(to_wxid: str, content: str, ats: str | None = None) -> dict:
    """发文本。群里 @ 人时 ats 为逗号分隔的 wxid（@所有人填 notify@all），content 里需含 @昵称。"""
    body = {"toWxid": to_wxid, "content": content}
    if ats:
        body["ats"] = ats
    return call("/gewe/v2/api/message/postText", body)


def post_link(to_wxid: str, title: str, desc: str, link_url: str, thumb_url: str) -> dict:
    """发链接卡片（doc.geweapi.com/api-139908318）。thumbUrl 必填，需是 gewe 能拉到的图片地址。"""
    return call("/gewe/v2/api/message/postLink", {"toWxid": to_wxid, "title": title, "desc": desc,
                                                  "linkUrl": link_url, "thumbUrl": thumb_url})


# ---- 联系人 ----
def post_image(to_wxid: str, img_url: str) -> dict:
    """发图（doc.geweapi.com/api-139908315）。imgUrl 必须是 gewe 服务器能拉到的公网地址，见 imagehost.py。
    返回 CDN 信息（aesKey/fileId/md5…），重复发同图可走 forwardImage。"""
    return call("/gewe/v2/api/message/postImage", {"toWxid": to_wxid, "imgUrl": img_url}, timeout=60)


def post_voice(to_wxid: str, voice_url: str, duration_ms: int) -> dict:
    """发语音（doc.geweapi.com/api-139908316）。voiceUrl 仅支持 silk（腾讯 silk 头 \\x02#!SILK_V3），须公网可拉；
    voiceDuration 单位毫秒，传真实时长。silk 生成/托管见 voice.py、imagehost.upload_silk。"""
    return call("/gewe/v2/api/message/postVoice",
                {"toWxid": to_wxid, "voiceUrl": voice_url, "voiceDuration": int(duration_ms)}, timeout=60)


def download_image(xml: str, img_type: int = 2) -> str:
    """下载图片（doc.geweapi.com/api-139908329）：xml=回调里的图片 XML，type 1高清/2常规/3缩略。返回 fileUrl（7 天有效）。"""
    return (call("/gewe/v2/api/message/downloadImage", {"xml": xml, "type": int(img_type)}, timeout=60) or {})["fileUrl"]


def download_voice(xml: str, msg_id: int) -> str:
    """下载语音（doc.geweapi.com/api-139908330）：xml=回调里的 voicemsg XML，msgId=回调 Data.MsgId。返回 silk 的 fileUrl。"""
    return (call("/gewe/v2/api/message/downloadVoice", {"xml": xml, "msgId": int(msg_id)}, timeout=60) or {})["fileUrl"]


def build_location_xml(lat: float, lng: float, label: str = "", poiname: str = "", scale: int = 15,
                       poiid: str = "") -> str:
    """按 postLocation 文档示例拼微信位置 XML。x=纬度、y=经度（GCJ-02）。"""
    from xml.sax.saxutils import quoteattr
    if not (-90 <= float(lat) <= 90 and -180 <= float(lng) <= 180):
        raise ValueError("经纬度越界")
    attrs = {"x": f"{float(lat):.6f}", "y": f"{float(lng):.6f}", "scale": str(int(scale)), "label": label or "",
             "maptype": "0", "poiname": poiname or "", "poiid": poiid or ""}
    inner = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
    return f'<?xml version="1.0"?>\n<msg>\n\t<location {inner} />\n</msg>\n'


def post_location(to_wxid: str, lat: float, lng: float, label: str = "", poiname: str = "", scale: int = 15) -> dict:
    """发送定位 /gewe/v2/api/message/postLocation（doc.geweapi.com/api-452601074）：参数 toWxid + content(位置 XML)。
    仅提供能力，不自动调用；真发仍须走白名单。"""
    return call("/gewe/v2/api/message/postLocation",
                {"toWxid": to_wxid, "content": build_location_xml(lat, lng, label, poiname, scale)}, timeout=30)


# ---- 朋友圈（doc.geweapi.com 朋友圈模块）----
# ⚠️ 官方提醒：新设备登录后 1-3 天内无法发布/点赞/评论朋友圈，期间操作会收到微信团队提醒。
# 发朋友圈属于对外公开发布：每一条都必须主人逐条批准后才可调用；代码里不自动调用。
def post_text_sns(content: str, privacy: bool = False, allow_wxids: list[str] | None = None,
                  at_wxids: list[str] | None = None, disable_wxids: list[str] | None = None,
                  allow_tag_ids: list[str] | None = None, disable_tag_ids: list[str] | None = None) -> dict:
    """发文字朋友圈 /gewe/v2/api/sns/sendTextSns（doc.geweapi.com/api-139908340）→ {id, userName, nickName, createTime}。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("朋友圈内容不能为空")
    body = {"content": content, "privacy": bool(privacy), "allowWxIds": list(allow_wxids or []),
            "atWxIds": list(at_wxids or []), "disableWxIds": list(disable_wxids or []),
            "allowTagIds": list(allow_tag_ids or []), "disableTagIds": list(disable_tag_ids or [])}
    return call("/gewe/v2/api/sns/sendTextSns", body, timeout=60)


def contacts_sns_list(wxid: str, max_id: int = 0, first_page_md5: str = "", decrypt: bool = True) -> dict:
    """只读：联系人的朋友圈 /gewe/v2/api/sns/contactsSnsList（非好友一般只能看到最近 10 条，第二页报错）。"""
    return call("/gewe/v2/api/sns/contactsSnsList",
                {"wxid": wxid, "maxId": max_id, "firstPageMd5": first_page_md5, "decrypt": decrypt}, timeout=60) or {}


def fetch_contacts() -> dict:
    """→ {friends:[wxid], chatrooms:[id@chatroom], ghs:[gh_...]}（大号可能较慢）"""
    return call("/gewe/v2/api/contacts/fetchContactsList", timeout=60) or {}


def get_brief_info(wxids: list[str]) -> list[dict]:
    """→ [{userName,nickName,remark,...}]，官方建议单次 ≤100 个。"""
    out: list[dict] = []
    for i in range(0, len(wxids), 100):
        out.extend(call("/gewe/v2/api/contacts/getBriefInfo", {"wxids": wxids[i:i + 100]}) or [])
    return out


# ---- 群（路径/字段按 doc.geweapi.com 群模块确认）----
def get_chatroom_info(chatroom_id: str) -> dict:
    """/group/getChatroomInfo → {chatroomId,nickName,remark,chatRoomOwner,smallHeadImgUrl,memberList:[...]}"""
    return call("/gewe/v2/api/group/getChatroomInfo", {"chatroomId": chatroom_id}) or {}


def get_chatroom_member_list(chatroom_id: str) -> dict:
    """/group/getChatroomMemberList → {memberList:[{wxid,nickName,inviterUserName,memberFlag,displayName,
    bigHeadImgUrl,smallHeadImgUrl}], chatroomOwner, adminWxid}"""
    return call("/gewe/v2/api/group/getChatroomMemberList", {"chatroomId": chatroom_id}) or {}


def get_chatroom_member_detail(chatroom_id: str, member_wxids: list[str]) -> list[dict]:
    """/group/getChatroomMemberDetail → [{userName,nickName,remark,signature,sex,country,province,city,...}]"""
    return call("/gewe/v2/api/group/getChatroomMemberDetail",
                {"chatroomId": chatroom_id, "memberWxids": member_wxids}) or []
