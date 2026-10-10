"""OpenAI 兼容接口：/v1/chat/completions、/v1/models。"""
import json
import time
import uuid
from typing import List, Optional, Union

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..model_catalog import to_openai_models
from ..tabbit_client import TabbitError

router = APIRouter()


class Message(BaseModel):
    role: str
    content: Union[str, list, None] = None
    reasoning_content: Optional[str] = None


class ChatRequest(BaseModel):
    model: str = "默认"
    messages: List[Message]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    # 兼容字段（忽略但接受）
    tools: Optional[list] = None
    tool_choice: Optional[object] = None


def setup(state) -> APIRouter:
    """state: {"auth": callable, "service": ChatService, "regions": {...}}"""
    auth = state["auth"]
    service = state["service"]

    @router.get("/v1/models")
    async def list_models(authorization: str = Header(default=""),
                          x_tabbit_region: str = Header(default=""),
                          region: str = ""):
        """列出模型。

        默认返回**所有已配置板块**的模型，ID 带板块前缀（cn/、intl/）
        以区分同名模型；可用 ?region=cn 只看某板块。
        请求头 X-Tabbit-Region 仍可用于限定默认板块。
        """
        auth(authorization)
        pool = state.get("pool")
        want = (region or "").strip().lower()
        if want in ("cn", "intl"):
            targets = [want]
        elif (x_tabbit_region or "").strip().lower() in ("cn", "intl"):
            targets = [(x_tabbit_region or "").strip().lower()]
        else:
            # 有账号的板块优先，同时包含已拉到目录的板块
            from ..model_catalog import regions_with_models
            have = regions_with_models()
            with_acc = [r for r in ("cn", "intl")
                        if pool is not None and pool.list(r)]
            targets = [r for r in ("cn", "intl")
                       if r in have or r in with_acc] or list(("cn", "intl"))
        return {"object": "list", "data": to_openai_models(targets)}

    @router.post("/v1/chat/completions")
    async def chat_completions(req: ChatRequest,
                               authorization: str = Header(default=""),
                               x_tabbit_region: str = Header(default="")):
        bearer = auth(authorization)
        region = _region(x_tabbit_region, state.get("pool"))
        content = _flatten(req.messages)
        if not content:
            raise HTTPException(400, "messages 不能为空")

        if req.stream:
            return StreamingResponse(
                _stream_sse(service, bearer, req.model, region, content),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        events, err = [], None
        try:
            async for ev in service.chat(bearer, req.model, region, content,
                                         endpoint="openai", stream=False,
                                         model_req=req.model):
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

        content_text, reasoning = _collect(events)
        usage = _usage(events, content_text)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content_text,
                            "reasoning_content": reasoning or None},
                "finish_reason": "stop",
            }],
            "usage": usage,
        }

    async def _stream_sse(service, bearer, model, region, content):
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        started = time.time()
        sent_reasoning = False
        try:
            async for ev in service.chat(bearer, model, region, content,
                                         endpoint="openai", stream=True,
                                         model_req=model):
                et, d = ev["event"], (ev["data"] or {})
                if et == "thinking":
                    r = d.get("reasoning_content")
                    if r and not sent_reasoning:
                        sent_reasoning = True
                        yield _sse(cid, created, model, {"role": "assistant",
                                   "content": "", "reasoning_content": ""})
                    if r:
                        yield _sse(cid, created, model, {"reasoning_content": r})
                elif et == "message_chunk":
                    c = d.get("content")
                    if isinstance(c, str) and c:
                        yield _sse(cid, created, model, {"content": c})
                elif et == "switch_model":
                    nm = d.get("new_model") or d.get("new_model_display_name")
                    if nm:
                        model = nm
                elif et == "error":
                    msg = d.get("message") or json.dumps(d, ensure_ascii=False)
                    yield f"data: {json.dumps({'error': {'message': f'Tabbit: {msg}', 'code': d.get('code', 500)}}, ensure_ascii=False)}\n\n"
                    break
                elif et in ("finish", "close", "cancel"):
                    break
            yield _sse(cid, created, model, {}, finish="stop")
            yield "data: [DONE]\n\n"
        except TabbitError as e:
            yield f"data: {json.dumps({'error': {'message': str(e), 'code': e.code}}, ensure_ascii=False)}\n\n"
        except Exception as e:  # 客户端断开等
            logger.debug("stream ended: %s", e)

    def _collect(events):
        content, reasoning = [], []
        for ev in events:
            d = ev.get("data") or {}
            if ev["event"] == "message_chunk" and isinstance(d.get("content"), str):
                content.append(d["content"])
            elif ev["event"] == "thinking" and isinstance(d.get("reasoning_content"), str):
                reasoning.append(d["reasoning_content"])
        return "".join(content), "".join(reasoning)

    def _usage(events, text):
        usage = None
        for ev in events:
            if ev["event"] == "usage":
                usage = ev["data"]
        total = _est(text)
        if isinstance(usage, dict):
            return {"prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                    "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens") or total,
                    "total_tokens": usage.get("total_tokens") or 0}
        return {"prompt_tokens": 0, "completion_tokens": total, "total_tokens": total}

    def _est(text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
        return max(1, cjk + (len(text) - cjk) // 4)

    return router


def _sse(cid, created, model, delta, finish=None):
    payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
               "model": model,
               "choices": [{"index": 0, "delta": delta,
                            "finish_reason": finish}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _flatten(messages: List[Message]) -> str:
    """把 OpenAI 消息列表拼成一段上游可用的纯文本 prompt（带长度预算）。

    上游对单次内容长度有硬限制（实测 ≥5 万字符返回 492），
    因此超长历史会被裁剪：保留系统指令 + 最近的对话。
    """
    from ..message_builder import build_from_openai
    return build_from_openai(messages)


def _region(header_value: str, pool=None) -> str:
    """解析板块：显式指定优先，否则落到**有账号的板块**。

    未显式指定时不应固定走 intl —— 否则账号在国内版时会报
    「国际版还没有账号」，开箱即用体验很差。
    """
    v = (header_value or "").strip().lower()
    if v in ("cn", "intl"):
        return v
    if pool is not None:
        for key in ("cn", "intl"):
            if pool.list(key):
                return key
    return "cn"


import logging  # noqa: E402
logger = logging.getLogger("tabbit2api")
