"""本机浏览器探测与登录启动。

Google OAuth 会拦截带自动化标志的浏览器：Playwright 默认传
`--enable-automation`，使 `navigator.webdriver === true`，登录页报
「此浏览器或应用可能不安全」。因此这里提供两条路径：

  1. `playwright` 加固模式：去掉 --enable-automation、加反检测参数、
     注入脚本抹掉 webdriver 指纹。
  2. `clean` 纯净模式：用**纯进程**方式启动浏览器（无任何自动化标志），
     用户登录完成后再通过 CDP 读取 Cookie —— 登录过程对 Google 完全正常。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows
    winreg = None

# exe 文件名 -> (id, 显示名, 是否 Chromium 内核可用 CDP)
KNOWN_EXES: dict[str, tuple[str, str, bool]] = {
    "Tabbit Browser.exe": ("tabbit", "Tabbit Browser", True),
    "chrome.exe": ("chrome", "Google Chrome", True),
    "msedge.exe": ("edge", "Microsoft Edge", True),
    "brave.exe": ("brave", "Brave", True),
    "vivaldi.exe": ("vivaldi", "Vivaldi", True),
    "opera.exe": ("opera", "Opera", True),
    "chromium.exe": ("chromium", "Chromium", True),
    "firefox.exe": ("firefox", "Mozilla Firefox", False),
}

FALLBACK_PATHS = [
    r"D:\Tabbit Browser\Application\Tabbit Browser.exe",
    r"C:\Program Files\Tabbit Browser\Application\Tabbit Browser.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"),
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]

# 反自动化检测参数
STEALTH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-features=ChromeWhatsNewUI,LockProfile",
    "--disable-component-update",
    "--disable-background-networking",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
if (!window.chrome) { window.chrome = {}; }
if (!window.chrome.runtime) { window.chrome.runtime = {}; }
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def _app_paths() -> dict[str, str]:
    """从注册表 App Paths 读取已安装程序（最权威的来源）。"""
    out: dict[str, str] = {}
    if not winreg:
        return out
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, sub) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        name = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, name) as sk:
                            val = winreg.QueryValue(sk, None)
                        if val:
                            out[name] = str(val).strip('"')
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _default_browser_progid() -> str:
    if not winreg:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\https\UserChoice",
        ) as k:
            return winreg.QueryValueEx(k, "ProgId")[0]
    except OSError:
        return ""


def detect_browsers() -> list[dict]:
    """返回本机可用于登录的浏览器列表（按可用性排序）。"""
    found: dict[str, dict] = {}   # 规范化路径 -> 信息
    app_paths = _app_paths()
    progid = _default_browser_progid().lower()

    def add(exe_name: str, path: str) -> None:
        info = KNOWN_EXES.get(exe_name) or KNOWN_EXES.get(os.path.basename(path))
        if not info:
            return
        bid, label, chromium = info
        try:
            rp = str(Path(path).resolve())
        except Exception:
            rp = path
        if not Path(rp).exists() or rp.lower() in found:
            return
        is_default = (
            (bid == "edge" and "mse" in progid)
            or (bid == "chrome" and "chrome" in progid)
            or (bid == "firefox" and "firefox" in progid)
            or (bid == "brave" and "brave" in progid)
            or (bid == "tabbit" and "tabbit" in progid)
        )
        try:
            ver = _file_version(rp)
        except Exception:
            ver = ""
        found[rp.lower()] = {
            "id": bid, "label": label, "path": rp,
            "chromium": chromium, "version": ver,
            "is_default": is_default,
            "recommended": chromium,
        }

    for exe_name, path in app_paths.items():
        add(exe_name, path)
    for p in FALLBACK_PATHS:
        if p:
            add(os.path.basename(p), p)

    order = {"tabbit": 0, "chrome": 1, "edge": 2, "brave": 3,
             "vivaldi": 4, "opera": 5, "chromium": 6, "firefox": 9}
    items = list(found.values())
    items.sort(key=lambda b: (order.get(b["id"], 7), not b["is_default"], b["label"]))
    return items


def _file_version(path: str) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item -LiteralPath '{path}').VersionInfo.FileVersion"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return out
    except Exception:
        return ""


def free_port(start: int = 9200, end: int = 9300) -> int:
    """为 CDP 调试端口找一个空闲端口。"""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用的调试端口")


def launch_clean(browser: dict, profile_dir: Path, url: str) -> tuple[subprocess.Popen, int]:
    """纯净模式：不带任何自动化标志启动浏览器，返回 (进程, CDP 端口)。

    登录过程完全由用户在真实浏览器中完成，Google 不会拦截；
    登录结束后我们再通过 CDP 读取 Cookie。
    """
    if not browser.get("chromium"):
        raise RuntimeError(f"{browser.get('label')} 不支持 CDP，无法自动读取登录态")
    port = free_port()
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        browser["path"],
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    creation = 0
    if os.name == "nt":
        creation = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, creationflags=creation, close_fds=True)
    # 等调试端口就绪
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{browser['label']} 启动后立即退出（返回码 {proc.returncode}）")
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1.5):
                return proc, port
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise RuntimeError(f"等待 {browser['label']} 调试端口超时")
