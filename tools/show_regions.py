import sys
sys.path.insert(0, '.')
from app.config import ConfigManager, region_for_base_url

c = ConfigManager()
for k in ("cn", "intl"):
    print(f"  {k}: {c.get('regions', k, 'base_url')}")
print()
for u in ("https://web.tabbit.com", "https://web.tabbit.ai",
          "https://web.tabbit-ai.com", "https://www.tabbit.ai"):
    print(f"  {u:32s} -> {region_for_base_url(u)}")
print()
for a in c.get("accounts", default=[]):
    print(f"  account {a.get('display_name')}: region={a.get('region')} base={a.get('base_url')}")
