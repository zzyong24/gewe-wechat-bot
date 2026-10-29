"""MiniMax 大模型客户端（OpenAI 兼容协议 /v1/chat/completions）。

文档：
- 国内站 https://platform.minimaxi.com/docs/guides/text-generation （文档示例域名 api.minimax.cn；api.minimaxi.com 亦可达）
- 海外站 https://platform.minimax.io/docs/guides/text-generation    （api.minimax.io）
- 参数   https://platform.minimax.io/docs/api-reference/text-openai-api

安全：key 只从环境变量 MINIMAX_API_KEY 读取，绝不打印/落盘；异常信息不含请求头、不含 key。

配置（环境变量，均可选）：
  MINIMAX_MODEL        默认 MiniMax-M3.1-Flash
  MINIMAX_BASE_URL     固定某个站点，如 https://api.minimaxi.com/v1；不设则按 DEFAULT_BASE_URLS 顺序尝试，
                       遇到鉴权类错误（401/1004 等，通常是 key 与站点区域不匹配）自动换下一个站点并记住可用的那个。
  MINIMAX_EFFORT       思考深度 low/medium/high/xhigh/max（M3.1-Flash 不能关思考；陪聊默认 low 以降低延迟）
  MINIMAX_TIMEOUT      秒，默认 60
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("llm.minimax")

DEFAULT_MODEL = "MiniMax-M3.1-Flash"
# sk-cp- 前缀 = Token Plan（订阅）Key；国内/海外 key 不通用，默认先试国内站。
DEFAULT_BASE_URLS = ("https://api.minimaxi.com/v1", "https://api.minimax.io/v1")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_session = requests.Session()        # keep-alive，省 TLS 握手


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, retriable: bool = False):
        super().__init__(msg)
        self.status, self.retriable = status, retriable


def _key() -> str:
    k = os.environ.get("MINIMAX_API_KEY", "").strip()
    i = k.find("sk-")            # 容错：值里可能带「api key 」之类的标签前缀
    if i > 0:
        k = k[i:].strip()
    if not k:
        raise LLMError("环境变量 MINIMAX_API_KEY 未设置")
    return k


@dataclass
class MiniMaxClient:
    model: str = field(default_factory=lambda: os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL))
    effort: str = field(default_factory=lambda: os.environ.get("MINIMAX_EFFORT", "low"))
    timeout: float = field(default_factory=lambda: float(os.environ.get("MINIMAX_TIMEOUT", "60")))
    max_completion_tokens: int = 2048       # 思考 token 也计入，太小会 finish_reason=length 且 content 为空
    temperature: float | None = None
    retries: int = 1                        # 网络错误 / 429 / 5xx 重试次数
    base_urls: tuple[str, ...] = field(default_factory=lambda: (
        (os.environ["MINIMAX_BASE_URL"].rstrip("/"),) if os.environ.get("MINIMAX_BASE_URL") else DEFAULT_BASE_URLS))
    last_usage: dict | None = field(default=None, init=False)
    last_base_url: str | None = field(default=None, init=False)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("MINIMAX_API_KEY", "").strip())

    def chat(self, user: str, system: str | None = None, history: list[dict] | None = None, **extra) -> str:
        """system 人设 + history（[{role:user/assistant, content}]）+ 本次 user 消息 → 回复文本。"""
        messages = ([{"role": "system", "content": system}] if system else []) + list(history or []) \
            + [{"role": "user", "content": user}]
        return self.complete(messages, **extra)

    def complete(self, messages: list[dict], **extra) -> str:
        body = {"model": self.model, "messages": messages, "reasoning_effort": self.effort,
                "max_completion_tokens": self.max_completion_tokens, **extra}
        if self.temperature is not None:
            body.setdefault("temperature", self.temperature)
        data = self._post("/chat/completions", body)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"响应结构异常: {str(data)[:200]}")
        self.last_usage = data.get("usage")
        text = _THINK_RE.sub("", text).strip()
        if not text:
            raise LLMError(f"模型返回空内容 (finish_reason={choice.get('finish_reason')})")
        return text

    def complete_raw(self, messages: list[dict], **extra) -> dict:
        """返回原始 assistant message（含 tool_calls），供工具调用循环用；content 里的 <think> 已去掉。"""
        body = {"model": self.model, "messages": messages, "reasoning_effort": self.effort,
                "max_completion_tokens": self.max_completion_tokens, **extra}
        data = self._post("/chat/completions", body)
        try:
            msg = dict(data["choices"][0]["message"])
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"响应结构异常: {str(data)[:200]}")
        self.last_usage = data.get("usage")
        msg["content"] = _THINK_RE.sub("", msg.get("content") or "").strip()
        return msg

    def chat_stream(self, user: str, system: str | None = None, history: list[dict] | None = None, **extra):
        """流式：逐块 yield 正文增量（不含思考内容）。只用已知可用站点；出错抛 LLMError（调用方可回退非流式）。"""
        messages = ([{"role": "system", "content": system}] if system else []) + list(history or []) \
            + [{"role": "user", "content": user}]
        body = {"model": self.model, "messages": messages, "reasoning_effort": self.effort,
                "max_completion_tokens": self.max_completion_tokens, "stream": True, **extra}
        if self.temperature is not None:
            body.setdefault("temperature", self.temperature)
        base = self.last_base_url or self.base_urls[0]
        headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
        try:
            r = _session.post(base + "/chat/completions", json=body, headers=headers, timeout=self.timeout, stream=True)
        except requests.RequestException as e:
            raise LLMError(f"网络错误: {type(e).__name__}", retriable=True) from None
        with r:
            if r.status_code != 200 or "event-stream" not in (r.headers.get("content-type") or ""):
                raise self._check(r) or LLMError(f"流式响应异常 http {r.status_code}", r.status_code)
            self.last_base_url = base
            import json as _json
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = _json.loads(data)
                except ValueError:
                    continue
                if j.get("usage"):
                    self.last_usage = j["usage"]
                piece = ((j.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                if piece:
                    yield piece

    # ---- 内部 ----
    def _post(self, path: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
        order = list(self.base_urls)
        if self.last_base_url in order:           # 已知可用站点优先
            order.remove(self.last_base_url)
            order.insert(0, self.last_base_url)
        err: LLMError | None = None
        for base in order:
            try:
                data = self._post_one(base + path, body, headers)
                self.last_base_url = base
                return data
            except LLMError as e:
                err = e
                if e.status in (401, 403, 1004, 2049):   # 鉴权/区域不匹配 → 换站点
                    log.warning("minimax %s 鉴权失败(%s)，尝试下一个站点", base, e.status)
                    continue
                raise
        raise err or LLMError("无可用站点")

    def _post_one(self, url: str, body: dict, headers: dict) -> dict:
        for attempt in range(self.retries + 1):
            try:
                r = _session.post(url, json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:      # 不把 headers/key 带进异常
                err = LLMError(f"网络错误: {type(e).__name__}", retriable=True)
            else:
                err = self._check(r)
                if err is None:
                    return r.json()
            if not err.retriable or attempt >= self.retries:
                raise err
            time.sleep(1.5 * (attempt + 1))
        raise err  # pragma: no cover

    @staticmethod
    def _check(r: requests.Response) -> LLMError | None:
        try:
            data = r.json()
        except ValueError:
            return LLMError(f"非 JSON 响应 (http {r.status_code})", r.status_code, r.status_code >= 500)
        base = data.get("base_resp") or {}
        code = base.get("status_code") or 0
        if r.status_code == 200 and not code:
            return None
        msg = base.get("status_msg") or (data.get("error") or {}).get("message") or str(data)[:200]
        status = code or r.status_code
        retriable = r.status_code == 429 or r.status_code >= 500 or code in (1002, 1039)   # 限流/超载
        return LLMError(f"MiniMax 错误 http={r.status_code} code={code}: {msg}", status, retriable)
