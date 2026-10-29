"""LLM 客户端。当前：MiniMax（OpenAI 兼容 chat/completions）。"""
from .minimax_client import MiniMaxClient, LLMError

__all__ = ["MiniMaxClient", "LLMError"]
