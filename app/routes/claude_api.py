"""Anthropic Claude 兼容接口：/v1/messages。"""
import json
import time
import uuid
from typing import List, Optional, Union

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..tabbit_client import TabbitError

router = APIRouter()


class ClaudeContent(BaseModel):
    type: str = "text"
    text: Optional[str] = None
    source: Optional[dict] = None


class ClaudeMessage(BaseModel):
    role: str
    content: Union[str, List[ClaudeContent], None] = None


class ClaudeRequest(BaseModel):
    model: str = "默认"
    messages: List[ClaudeMessage]
    system: Union[str, list, None] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None


def setup(state) -> APIRouter:
    auth = state["auth"]
    service = state["service"]

    @router.post("/v1/messages")
    async def messages(req: ClaudeRequest,
                       authorization: str = Header(default=""),
                       x_api_key: str = Header(default=""),
                       anthropic_version: str = Header(default=""),
                       x_tabbit_region: str = Header(default="")):
        bearer = auth(authorization or (f"Bearer {x_api_key}" if x_api_key else ""))
        region = _region(x_tabbit_region, state.get("pool"))
        content = _flatten(req.system, req.messages)
        if not content:
            raise HTTPException(400, "messages 不能为空")

        if req.stream:
            return StreamingResponse(
                _stream(service, bearer, req.model, region, content),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        events, err = [], None
        try:
            async for ev in service.chat(bearer, req.model, region, content):
                events.append(ev)
                if ev["event"] == "error":
                    err = ev["data"]
                    break
        except TabbitError as e:
            raise HTTPException(e.code, str(e))
        if err:
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
            code = int(err.get("code") or 500)
            raise HTTPException(502 if code < 400 else code, f"Tabbit: {msg}")

        text, reasoning = _collect(events)
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": req.model,
            "content": ([{"type": "thinking", "thinking": reasoning}] if reasoning else [])
                       + [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": _est(text)},
        }

    async def _stream(service, bearer, model, region, content):
        mid = f"msg_{uuid.uuid4().hex[:24]}"
        started = time.time()
        block_idx = 0
        in_thinking = False
        in_text = False

        def ev(name: str, data: dict) -> str:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            yield ev("message_start", {
                "type": "message_start",
                "message": {"id": mid, "type": "message", "role": "assistant",
                            "model": model, "content": [],
                            "stop_reason": None, "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}})
            async for item in service.chat(bearer, model, region, content):
                et, d = item["event"], (item["data"] or {})
                if et == "thinking":
                    r = d.get("reasoning_content")
                    if not r:
                        continue
                    if not in_thinking:
                        in_thinking = True
                        yield ev("content_block_start",
                                 {"type": "content_block_start", "index": block_idx,
                                  "content_block": {"type": "thinking", "thinking": ""}})
                    yield ev("content_block_delta",
                             {"type": "content_block_delta", "index": block_idx,
                              "delta": {"type": "thinking_delta", "thinking": r}})
                elif et == "message_chunk":
                    c = d.get("content")
                    if isinstance(c, str) and c:
                        if in_thinking:
                            yield ev("content_block_stop",
                                     {"type": "content_block_stop", "index": block_idx})
                            block_idx += 1
                            in_thinking = False
                        if not in_text:
                            in_text = True
                            yield ev("content_block_start",
                                     {"type": "content_block_start", "index": block_idx,
                                      "content_block": {"type": "text", "text": ""}})
                        yield ev("content_block_delta",
                                 {"type": "content_block_delta", "index": block_idx,
                                  "delta": {"type": "text_delta", "text": c}})
                elif et == "error":
                    msg = d.get("message") or json.dumps(d, ensure_ascii=False)
                    yield ev("error", {"type": "error",
                                       "error": {"type": "api_error",
                                                 "message": f"Tabbit: {msg}"}})
                    return
                elif et in ("finish", "close", "cancel"):
                    break
            if in_thinking or in_text:
                yield ev("content_block_stop",
                         {"type": "content_block_stop", "index": block_idx})
            yield ev("message_delta", {"type": "message_delta",
                                       "delta": {"stop_reason": "end_turn",
                                                 "stop_sequence": None},
                                       "usage": {"output_tokens": 0}})
            yield ev("message_stop", {"type": "message_stop"})
        except TabbitError as e:
            yield ev("error", {"type": "error",
                               "error": {"type": "api_error", "message": str(e)}})

    def _collect(events):
        text, reasoning = [], []
        for ev in events:
            d = ev.get("data") or {}
            if ev["event"] == "message_chunk" and isinstance(d.get("content"), str):
                text.append(d["content"])
            elif ev["event"] == "thinking" and isinstance(d.get("reasoning_content"), str):
                reasoning.append(d["reasoning_content"])
        return "".join(text), "".join(reasoning)

    def _est(text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
        return max(1, cjk + (len(text) - cjk) // 4)

    return router


def _region(v: str, pool=None) -> str:
    """解析板块：显式指定优先，否则落到有账号的板块（见 openai_api._region）。"""
    v = (v or "").strip().lower()
    if v in ("cn", "intl"):
        return v
    if pool is not None:
        for key in ("cn", "intl"):
            if pool.list(key):
                return key
    return "cn"


def _flatten(system, messages: List[ClaudeMessage]) -> str:
    parts: list[str] = []
    if system:
        if isinstance(system, str):
            parts.append(f"[系统指令]\n{system}")
        else:
            buf = []
            for seg in system:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    buf.append(seg.get("text", ""))
            if buf:
                parts.append("[系统指令]\n" + "\n".join(buf))
    for m in messages:
        c = m.content
        if isinstance(c, list):
            buf = []
            for seg in c:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        buf.append(seg.get("text", ""))
                    elif seg.get("type") == "image":
                        buf.append("[图片：当前实现暂不支持图片输入]")
            c = "\n".join(x for x in buf if x)
        if not isinstance(c, str):
            c = "" if c is None else str(c)
        if m.role == "assistant":
            parts.append(f"[助手]\n{c}")
        else:
            parts.append(c)
    return "\n\n".join(p for p in parts if p.strip())
