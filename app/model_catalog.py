"""模型目录：请求别名 → 上游 display_name，并支持板块前缀。

上游 `selected_model` 接受的是模型列表里的 `display_name`
（实测：发 slug 会被服务端「自动切换」到别的模型），所以这里
把常见别名规范化映射到真实 display_name，其余直接原样匹配。

**板块前缀**：国内版与国际版有 14 个同名模型（GLM-5.3、Kimi-K3 等），
不加区分客户端无法选择。因此对外模型 ID 采用 `<板块>/<slug>` 形式：

    cn/glm-5.3        国内版
    intl/gpt-6-sol    国际版

同时保留**裸名**（如 `glm-5.3`）作为兼容别名，按「有账号的板块」解析，
使既有配置不失效。解析优先级：
    1. 显式前缀（cn/xxx、intl/xxx）
    2. 当前板块内精确匹配
    3. 任一板块内精确匹配（唯一命中才采用）
    4. 裸名歧义（多板块同名）→ 报错并提示用前缀
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

from .config import REGION_PRESETS

_lock = threading.Lock()
# region -> {"models": [...], "updated_at": float}
_catalog: dict[str, dict] = {}

REGION_KEYS = tuple(REGION_PRESETS.keys())          # ("cn", "intl")
# 常见板块前缀写法 → 规范 key
_PREFIX_ALIASES = {
    "cn": "cn", "china": "cn", "domestic": "cn", "国内": "cn", "国内版": "cn",
    "intl": "intl", "international": "intl", "overseas": "intl",
    "global": "intl", "国际": "intl", "国际版": "intl",
}


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


def regions_with_models() -> list[str]:
    return [r for r in REGION_KEYS if all_models(r)]


# ── 前缀处理 ─────────────────────────────────────────────────────
def split_prefix(name: str) -> tuple[Optional[str], str]:
    """拆分 `cn/glm-5.3` → ("cn", "glm-5.3")；无前缀返回 (None, 原名)。"""
    raw = (name or "").strip()
    if "/" not in raw:
        return None, raw
    head, _, tail = raw.partition("/")
    key = _PREFIX_ALIASES.get(head.strip().lower())
    if key and tail.strip():
        return key, tail.strip()
    return None, raw


def with_prefix(region: str, model_id: str) -> str:
    return f"{region}/{model_id}"


# ── 查找 ─────────────────────────────────────────────────────────
def _match_in(region: str, name: str) -> Optional[dict]:
    n = _norm(name)
    if not n:
        return None
    for m in all_models(region):
        if _norm(m.get("display_name", "")) == n:
            return m
        if _norm(_slug(m.get("display_name", ""))) == n:
            return m
    aliases = {"best", "default", "free", "默认", "最佳", "mo-ren", "zui-jia"}
    if n in {_norm(a) for a in aliases}:
        for m in all_models(region):
            if m.get("model_access_type") == "free_unlimited":
                return m
    return None


def find_model(region: str, name: str) -> tuple[Optional[dict], Optional[str], str]:
    """解析模型名。

    返回 (model_obj, region_key, error_message)。
    region 为调用方期望的板块（可能为空/None）。
    """
    prefix, bare = split_prefix(name)

    # 1) 显式前缀：只在该板块找
    if prefix:
        m = _match_in(prefix, bare)
        if m:
            return m, prefix, ""
        return None, prefix, (f"「{REGION_PRESETS[prefix]['label']}」板块没有模型 "
                              f"{bare}（可用 GET /v1/models 查看）")

    # 2) 期望板块内匹配
    if region:
        m = _match_in(region, bare)
        if m:
            return m, region, ""

    # 3) 其他板块唯一命中
    hits = [(r, m) for r in REGION_KEYS
            if r != region and (m := _match_in(r, bare))]
    if len(hits) == 1:
        return hits[0][1], hits[0][0], ""

    # 4) 多板块同名 → 要求带前缀
    if len(hits) > 1:
        names = "、".join(REGION_PRESETS[r]["label"] for r, _ in hits)
        return None, None, (
            f"模型 {bare} 在多个板块都存在（{names}），请加前缀区分："
            + "、".join(f"{r}/{_slug(m.get('display_name',''))}"
                        for r, m in hits))

    return None, None, f"未知模型：{name}"


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").strip()).lower()


# 纯中文 display_name → 对外英文 ID（避免客户端出现中文模型名）
_CN_ALIAS = {
    "默认": "default",
    "最佳": "best",
}


def _slug(display_name: str) -> str:
    """GLM-5.3 → glm-5-3；纯中文名走别名表，兜底保留原值。"""
    d = (display_name or "").strip()
    if d in _CN_ALIAS:
        return _CN_ALIAS[d]
    s = d.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-\.]+", "-", s).strip("-")
    if not s:
        # 全中文且无别名：用稳定的拼音式短标识（md5 前 8 位）保证可用
        import hashlib as _h
        return "cn-" + _h.md5(d.encode()).hexdigest()[:8]
    return s


# ── 对外模型列表 ─────────────────────────────────────────────────
def to_openai_models(regions: Optional[list[str]] = None,
                     prefix: bool = True) -> list[dict]:
    """汇总各板块模型。

    prefix=True 时 id 形如 `cn/glm-5.3`，用于区分同名模型。
    """
    targets = regions or list(REGION_KEYS)
    out: list[dict] = []
    for region in targets:
        cat = get_catalog(region)
        label = REGION_PRESETS.get(region, {}).get("label", region)
        for m in cat.get("models") or []:
            slug = _slug(m.get("display_name", "")) or m.get("display_name", "")
            out.append({
                "id": with_prefix(region, slug) if prefix else slug,
                "object": "model",
                "created": int(cat.get("updated_at") or time.time()),
                "owned_by": "tabbit",
                # 兼容/展示用扩展字段
                "display_name": m.get("display_name"),
                "region": region,
                "region_label": label,
                "access_type": m.get("model_access_type"),
                "supports_images": m.get("supports_images"),
                "supports_tools": m.get("supports_tools"),
                "supports_thinking": m.get("support_thinking"),
                "multiplier": m.get("display_multiplier"),
            })
    return out