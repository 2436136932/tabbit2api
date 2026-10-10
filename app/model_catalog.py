"""模型目录：请求别名 → 上游 display_name。

上游 `selected_model` 接受的是模型列表里的 `display_name`
（实测：发 slug 会被服务端「自动切换」到别的模型），所以这里
把常见别名规范化映射到真实 display_name，其余直接原样匹配。

每个区域板块维护一份从上游拉到的真实模型列表（含
model_access_type / display_multiplier），供 /v1/models 与面板展示。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

_lock = threading.Lock()
# region -> {"models": [...], "updated_at": float}
_catalog: dict[str, dict] = {}


def set_models(region: str, models: list[dict]) -> None:
    with _lock:
        _catalog[region] = {"models": models, "updated_at": time.time()}


def get_catalog(region: str) -> dict:
    with _lock:
        c = _catalog.get(region)
        if c:
            return dict(c)
    return {"models": [], "updated_at": None}


def all_models(region: str) -> list[dict]:
    return get_catalog(region).get("models") or []


def find_model(region: str, name: str) -> Optional[dict]:
    n = _norm(name)
    if not n:
        return None
    for m in all_models(region):
        if _norm(m.get("display_name", "")) == n:
            return m
        if _norm(_slug(m.get("display_name", ""))) == n:
            return m
    # 别名表（默认/最佳 → free_unlimited 那个）
    aliases = {"best", "default", "free", "默认", "最佳", "mo-ren", "zui-jia"}
    if n in {_norm(a) for a in aliases}:
        for m in all_models(region):
            if m.get("model_access_type") == "free_unlimited":
                return m
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").strip()).lower()


def _slug(display_name: str) -> str:
    """GLM-5.3 → glm-5-3；默认 → best 兜底不在此处理。"""
    return (display_name or "").strip().lower().replace(" ", "-")


def to_openai_models(region: str) -> list[dict]:
    out = []
    for m in all_models(region):
        out.append({
            "id": _slug(m.get("display_name", "")) or m.get("display_name", ""),
            "object": "model",
            "created": int(get_catalog(region).get("updated_at") or time.time()),
            "owned_by": "tabbit",
            "display_name": m.get("display_name"),
            "access_type": m.get("model_access_type"),
            "supports_images": m.get("supports_images"),
            "supports_tools": m.get("supports_tools"),
            "supports_thinking": m.get("support_thinking"),
            "multiplier": m.get("display_multiplier"),
        })
    return out
