"""增量消息计算：从客户端每次重发的完整历史中提取「新增部分」。

Agent 客户端（DSH / Cline / Roo 等）每轮都会把**完整对话历史**发过来，
长任务下累积上百万字符，必然超过上游约 2 万字符的单次上限。

由于上游会话本身保留上下文（实测：同一会话追问前文信息可正确回答），
因此可以只把**新增的消息**发给上游，历史交给上游记忆维持。

策略（保守优先，宁可多发不可漏发）：
  1. 计算本次消息列表相对「上次已发送」的最长公共前缀
  2. 若上次的历史仍是本次的前缀 → 只发新增部分（增量）
  3. 若历史被客户端改写（如压缩/编辑）→ 无法安全增量，
     退回「发完整内容（带长度预算裁剪）」
  4. 增量结果若为空（客户端重复发送同一请求）→ 发最后一条用户消息
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger("tabbit2api")


def fingerprint(role: str, text: str) -> str:
    """单条消息指纹（角色 + 内容）。"""
    h = hashlib.sha1()
    h.update((role or "").encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update((text or "").encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def fingerprints(items: list[tuple[str, str]]) -> list[str]:
    return [fingerprint(r, t) for r, t in items]


def common_prefix_len(a: list[str], b: list[str]) -> int:
    """最长公共前缀长度。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class IncrementalTracker:
    """按会话键记录已发送的消息指纹，计算增量。

    状态保存在内存中，随会话 TTL 一起失效。
    """

    def __init__(self) -> None:
        # key -> {"fps": [...], "at": float}
        self._sent: dict[str, dict] = {}

    def plan(self, key: str, items: list[tuple[str, str]]
             ) -> tuple[list[tuple[str, str]], str]:
        """决定本次要发送的消息。

        返回 (要发送的消息列表, 模式说明)。
        模式：incremental / full / resend_last
        """
        fps = fingerprints(items)
        st = self._sent.get(key)

        if not st:
            self.remember(key, fps)
            return items, "full"

        prev: list[str] = st.get("fps") or []
        plen = common_prefix_len(prev, fps)

        # 上次历史完整保留为前缀，且本次确实更长 → 增量
        if plen >= len(prev) and len(fps) > len(prev):
            new_items = items[len(prev):]
            self.remember(key, fps)
            return new_items, "incremental"

        # 历史被改写（压缩/编辑）或变短 → 无法安全增量
        if plen < len(prev):
            self.remember(key, fps)
            return items, "full"

        # 完全相同的重复请求 → 重发最后一条用户消息
        last_user = None
        for role, text in reversed(items):
            if role == "user":
                last_user = (role, text)
                break
        if last_user:
            return [last_user], "resend_last"
        return items, "full"

    def remember(self, key: str, fps: list[str]) -> None:
        self._sent[key] = {"fps": fps, "at": __import__("time").time()}

    def forget(self, key: str) -> None:
        self._sent.pop(key, None)

    def clear(self) -> int:
        n = len(self._sent)
        self._sent.clear()
        return n

    def stats(self) -> list[dict]:
        import time as _t
        now = _t.time()
        return [{"key": k, "messages": len(v.get("fps") or []),
                 "age": round(now - v.get("at", now), 1)}
                for k, v in self._sent.items()]