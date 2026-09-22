"""LLM 调用层。所有对模型的调用必须经由 ``client``,这是 P3 的落地点。"""
from .client import Completion, Prompt, call, load_prompt, record  # noqa: F401
