"""消息拼装与长度预算控制。

上游对**单次请求内容长度**有硬限制：实测 ≥ 5 万字符即返回
`error code=492`（文案为「欢迎使用 Tabbit 浏览器…」，与真实原因无关，
极具误导性）。国内版与国际版表现一致。

因此不能把整段对话历史无脑拼成一条消息。本模块按预算裁剪：
  1. 系统指令优先保留（截断到限额）
  2. 从**最近**的消息往回填，直到用满预算
  3. 单条超长消息截断保留首尾（中间省略），保留头尾信息
  4. 明确告知模型「历史已截断」，避免它以为对话就这么多

预算默认 24000 字符（远低于 5 万阈值，留足安全余量）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("tabbit2api")

# 实测阈值：20000 字符成功、25000 字符必失败（与内容形态无关，
# 自然文本与无空格长串表现一致）。取 16000 作默认预算，留足安全余量
# （中文按字符计，UTF-8 下字节数约为 3 倍，上游可能按字节或 token 判定）。
DEFAULT_BUDGET = 16000
MAX_SINGLE = 9000          # 单条消息上限
TRUNC_HEAD = 6000          # 超长单条保留的开头
TRUNC_TAIL = 2000          # 超长单条保留的结尾

_TRUNC_NOTE = "\n…[内容过长，中间部分已省略]…\n"
_HISTORY_NOTE = "[注意：更早的对话历史因长度限制已省略]"


def clip(text: str, limit: int = MAX_SINGLE) -> str:
    """把单条文本裁剪到 limit 以内，保留首尾。"""
    if len(text) <= limit:
        return text
    head = max(1, min(TRUNC_HEAD, limit - len(_TRUNC_NOTE) - 1))
    tail = max(0, min(TRUNC_TAIL, limit - len(_TRUNC_NOTE) - head))
    return text[:head] + _TRUNC_NOTE + (text[-tail:] if tail else "")


def render(role: str, text: str) -> str:
    """按角色加标记（上游看到的是纯文本，需要角色提示）。"""
    if role == "system":
        return f"[系统指令]\n{text}"
    if role == "assistant":
        return f"[助手]\n{text}"
    if role == "tool":
        return f"[工具结果]\n{text}"
    return text


def _clip_system(text: str, limit: int) -> str:
    """裁剪系统指令但保住 `[系统指令]` 前缀（前缀本身占预算）。"""
    prefix = "[系统指令]\n"
    room = max(100, limit - len(prefix))
    return prefix + clip(text, room)


def build(items: list[tuple[str, str]], budget: int = DEFAULT_BUDGET) -> str:
    """把 [(role, text)] 拼成一条不超预算的 prompt。

    保留顺序：system 全部 → 从最新往回累积其它消息 → 按时间正序输出。
    """
    items = [(r, (t or "").strip()) for r, t in items if (t or "").strip()]
    if not items:
        return ""

    systems = [t for r, t in items if r == "system"]
    others = [(r, t) for r, t in items if r != "system"]

    # 预算分配：系统指令最多占 1/3，且**先从总预算中扣除**，
    # 保证后续填充不会把它挤出预算（否则兜底裁剪会吃掉系统指令）
    sys_budget = max(500, budget // 3)
    sys_text = "\n\n".join(_clip_system(s, sys_budget) for s in systems)
    used = len(sys_text)
    body_budget = max(200, budget - used)      # 留给对话历史的预算

    # 从最近往回填（用 body_budget，不动系统指令的位置）
    kept: list[str] = []
    dropped = 0
    body_used = 0
    for role, text in reversed(others):
        block = render(role, clip(text))
        if body_used + len(block) + 2 > body_budget:
            dropped += 1
            continue
        kept.append(block)
        body_used += len(block) + 2
    kept.reverse()

    if dropped:
        kept.insert(0, _HISTORY_NOTE)

    parts = ([sys_text] if sys_text else []) + kept
    out = "\n\n".join(p for p in parts if p.strip())

    # 兜底：仍超预算时只裁对话部分，保留系统指令
    if len(out) > budget:
        room = max(200, budget - len(sys_text) - 4)
        body = "\n\n".join(kept)
        body = clip(body, room)
        out = (sys_text + "\n\n" + body) if sys_text else body
    if dropped:
        logger.info("消息超长：省略 %d 条较早消息，最终 %d 字符", dropped, len(out))
    return out


def build_from_openai(messages, budget: int = DEFAULT_BUDGET) -> str:
    """OpenAI 风格 messages → 单条 prompt。"""
    items: list[tuple[str, str]] = []
    for m in messages:
        c = getattr(m, "content", None)
        role = getattr(m, "role", "user") or "user"
        if isinstance(c, list):
            buf = []
            for seg in c:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        buf.append(seg.get("text", ""))
                    elif seg.get("type") == "image_url":
                        buf.append("[图片：当前实现暂不支持图片输入]")
                elif isinstance(seg, str):
                    buf.append(seg)
            c = "\n".join(x for x in buf if x)
        if not isinstance(c, str):
            c = "" if c is None else str(c)
        items.append((role, c))
    return build(items, budget)


def build_from_claude(system, messages, budget: int = DEFAULT_BUDGET) -> str:
    """Claude 风格 system + messages → 单条 prompt。"""
    items: list[tuple[str, str]] = []
    if system:
        if isinstance(system, str):
            items.append(("system", system))
        else:
            buf = []
            for seg in system:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    buf.append(seg.get("text", ""))
            if buf:
                items.append(("system", "\n".join(buf)))
    for m in messages:
        c = getattr(m, "content", None)
        role = getattr(m, "role", "user") or "user"
        if isinstance(c, list):
            buf = []
            for seg in c:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        buf.append(seg.get("text", ""))
                    elif seg.get("type") == "image":
                        buf.append("[图片：当前实现暂不支持图片输入]")
                elif isinstance(seg, str):
                    buf.append(seg)
            c = "\n".join(x for x in buf if x)
        if not isinstance(c, str):
            c = "" if c is None else str(c)
        items.append((role, c))
    return build(items, budget)