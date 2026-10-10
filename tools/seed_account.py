"""开发辅助：把浏览器导出的 Cookie 直接写入账号池（跳过浏览器 UI）。

用法：python tools/seed_account.py <cookie.json> <region> [base_url]
仅用于自测；正常导入请用管理面板的「浏览器登录」。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.accounts import Account  # noqa: E402
from app.config import ConfigManager, REGION_PRESETS  # noqa: E402
from app.model_catalog import set_models  # noqa: E402
from app.tabbit_client import TabbitClient, TabbitError  # noqa: E402


async def main() -> int:
    cookie_path = sys.argv[1]
    region = sys.argv[2] if len(sys.argv) > 2 else "intl"
    base_url = (sys.argv[3] if len(sys.argv) > 3
                else REGION_PRESETS.get(region, {}).get("base_url"))
    raw = Path(cookie_path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    token = data["token"]
    uid = data.get("userId") or data.get("user_id") or ""

    cfg = ConfigManager()
    from app.accounts import AccountPool
    pool = AccountPool(cfg)

    probe = TabbitClient(base_url=base_url, token=token, user_id=uid)
    try:
        base_info = await probe.get_base_info()
    except TabbitError:
        base_info = {}
    try:
        quota = await probe.get_quota()
    except TabbitError:
        quota = {}
    try:
        models = await probe.get_models()
    except TabbitError:
        models = []
    await probe.aclose()

    display = base_info.get("display_name") or "Tabbit 账号"
    mask = base_info.get("secret_mobile") or base_info.get("secret_email") or ""
    plans = quota.get("membership_plan_details") or []
    blob = json.dumps({"token": token, "user_id": uid,
                       "device_id": "", "next_auth": ""})
    acc = Account({
        "name": display,
        "region": region,
        "base_url": base_url,
        "user_id": uid,
        "token_cipher": cfg.encrypt_token(blob),
        "display_name": display,
        "account_mask": mask,
        "member_level": quota.get("member_level", ""),
        "plan_name": plans[0].get("plan_display_name", "") if plans else "",
        "usage_percentage": quota.get("usage_percentage", ""),
        "quota": quota,
        "models": models,
        "quota_updated_at": __import__("time").time(),
        "status": "active",
    })
    # 复用 client 的 user_id 解析逻辑补齐 device_id
    from app.tabbit_client import TabbitClient as _C
    acc.device_id = _C(base_url, "x", user_id=uid).device_id
    pool.add(acc)
    set_models(region, models)
    print(f"seeded account {acc.id} display={display} mask={mask} "
          f"member={acc.member_level} models={len(models)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
