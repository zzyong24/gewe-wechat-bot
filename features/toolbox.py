"""陪聊模型的统一工具层：把各能力（高德地图、联网搜索、生图…）的 function-calling 工具汇总，按群开关下发。
每个 provider 提供：names(set)、specs(list)、available(sess)、wants(text, last_user)、run(name, args, sess, wxid)、ack(bool)。
只有本群可用、且这句话像是用得上的 provider 才把工具带给模型（工具调用要多一轮模型，闲聊不付这个延迟）。
"""
from __future__ import annotations


class Toolbox:
    def __init__(self, providers):
        self.providers = [p for p in providers if p is not None]

    @property
    def specs(self) -> list[dict]:
        return [s for p in self.providers for s in p.specs]

    def available(self, sess: str) -> bool:
        return any(p.available(sess) for p in self.providers)

    def select(self, sess: str, text: str, history: list[dict] | None = None) -> list[dict]:
        last_user = next((m["content"] for m in reversed(history or []) if m.get("role") == "user"), "")
        out = []
        for p in self.providers:
            try:
                if p.available(sess) and p.wants(text, last_user):
                    out.extend(p.specs)
            except Exception:
                continue
        return out

    def ack_for(self, names) -> bool:
        names = set(names)
        return any(getattr(p, "ack", True) and (names & set(p.names)) for p in self.providers)

    def run(self, name: str, args: dict, sess: str, wxid: str) -> str:
        for p in self.providers:
            if name in p.names:
                return p.run(name, args, sess, wxid)
        return f"没有叫 {name} 的工具"
