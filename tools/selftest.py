"""开发自测脚本：用浏览器导出的 Cookie 直接驱动 TabbitClient，
验证 HMAC 签名、会话创建与 SSE 流在纯 Python（无浏览器）下可用。

用法：python tools/selftest.py <cookie.json> [base_url]
cookie.json 形如 {"token":"...","userId":"..."}
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tabbit_client import TabbitClient, TabbitError  # noqa: E402


async def main() -> int:
    cookie_path = sys.argv[1] if len(sys.argv) > 1 else ""
    base_url = sys.argv[2] if len(sys.argv) > 2 else "https://web.tabbit.com"
    if not cookie_path or not Path(cookie_path).exists():
        print("用法: python tools/selftest.py <cookie.json> [base_url]")
        return 2
    data = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
    token = data.get("token") or data.get("token_value") or ""
    uid = data.get("userId") or data.get("user_id") or ""
    if not token:
        print("cookie 文件缺少 token")
        return 2

    c = TabbitClient(base_url=base_url, token=token, user_id=uid)
    ok = True
    try:
        models = await c.get_models()
        print(f"[1] 模型列表 OK  {len(models)} 个")
        names = [m.get("display_name") for m in models][:5]
        print("    示例:", names)

        info = await c.get_base_info()
        print(f"[2] 用户信息 OK  {info.get('display_name')} ({info.get('secret_mobile')})")

        quota = await c.get_quota()
        print(f"[3] 真实额度 OK  member_level={quota.get('member_level')} "
              f"used={quota.get('usage_percentage')}")

        target = next((m["display_name"] for m in models
                       if m.get("model_access_type") == "free_unlimited"),
                      models[0]["display_name"] if models else "默认")
        sid = await c.create_session()
        print(f"[4] 创建会话 OK  {sid}")

        content, reasoning = [], []
        async for ev in c.stream_chat(sid, "用三个字回复：测试通过", target):
            et, d = ev["event"], ev["data"] or {}
            if et == "message_chunk" and isinstance(d.get("content"), str):
                content.append(d["content"])
            elif et == "thinking" and isinstance(d.get("reasoning_content"), str):
                reasoning.append(d["reasoning_content"])
            elif et == "error":
                print(f"[5] 上游错误 code={d.get('code')} {d.get('message')}")
                ok = False
            elif et in ("finish", "close", "cancel"):
                break
        text = "".join(content)
        print(f"[5] 对话 SSE OK  回复={text!r} 思考长度={len(''.join(reasoning))}")
        if not text:
            ok = False
    except TabbitError as e:
        print(f"FAIL TabbitError code={e.code}: {e}")
        ok = False
    finally:
        await c.aclose()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
