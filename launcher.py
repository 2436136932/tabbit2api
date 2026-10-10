"""启动器：供 启动.bat / 结束.bat 调用，负责进程生命周期与端口探测。

把逻辑放在 Python 而不是 BAT 里，是因为中文 BAT 文件在 cmd 下会因
UTF-8 多字节字符被按字节误解析（echo 行被当命令、延迟展开失效、
PowerShell 命令行被截断）。BAT 只做最小转发。

用法：
    python launcher.py start     启动服务（独立窗口）并就绪后打开面板
    python launcher.py stop      停止服务
    python launcher.py restart   重启
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".server.pid"
PORT_FILE = ROOT / ".server.port"
START_PORT = 8123
MAX_PORTS = 15
READY_TIMEOUT = 25  # 秒

CREATE_NEW_CONSOLE = 0x00000010  # Windows: 独立控制台窗口
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


# ── 工具 ────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """Windows 原生存活检查。

    不用 os.kill(pid, 0)：跨进程检查 CREATE_NEW_CONSOLE 启动的进程时
    实测不可靠（会误报不存在），导致重复启动/停止判断失误。
    """
    if pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return True  # 能拿到句柄即视为存活
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="ascii").strip())
    except Exception:
        return None


def _health(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return '"ok":true' in r.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _find_running_port() -> int | None:
    for port in range(START_PORT, START_PORT + MAX_PORTS):
        if _health(port):
            return port
    return None


def _kill_pid_tree(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    try:
        subprocess.run(["taskkill", "/pid", str(pid), "/t", "/f"],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    return True


def _kill_by_commandline() -> int:
    """兜底：按命令行匹配 python start.py 并结束（处理 PID 文件丢失）。"""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and "
        "$_.CommandLine -like '*start.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return 0
    return len([ln for ln in out.splitlines() if ln.strip().isdigit()])


# ── 命令 ────────────────────────────────────────────────────────
def cmd_start() -> int:
    print("=" * 52)
    print("  Tabbit2API 启动中...")
    print("=" * 52)

    # 已在运行？
    pid = _read_pid()
    if pid and _pid_alive(pid):
        port = _find_running_port()
        print(f"[i] 服务已在运行（PID {pid}），无需重复启动。")
        _report(port)
        _open_panel(port)
        return 0

    # 端口被别的东西占用但服务其实是我们的（PID 文件丢失）
    existing = _find_running_port()
    if existing:
        print(f"[i] 检测到服务已在端口 {existing} 运行（PID 文件缺失）。")
        _report(existing)
        _open_panel(existing)
        return 0

    # 启动
    print("[1/3] 正在启动服务（会打开一个服务窗口显示日志）...")
    try:
        proc = subprocess.Popen(
            [sys.executable, "start.py"],
            cwd=str(ROOT),
            creationflags=CREATE_NEW_CONSOLE,
        )
    except Exception as e:
        print(f"[x] 启动失败：{e}")
        return 1
    PID_FILE.write_text(str(proc.pid), encoding="ascii")
    print(f"      服务进程 PID = {proc.pid}")

    # 等待就绪
    print("[2/3] 等待服务就绪...")
    deadline = time.time() + READY_TIMEOUT
    port = None
    while time.time() < deadline:
        port = _find_running_port()
        if port:
            break
        if proc.poll() is not None:
            print("[x] 服务进程已退出，请查看服务窗口中的报错信息。")
            PID_FILE.unlink(missing_ok=True)
            return 1
        time.sleep(0.7)

    if not port:
        print("[x] 等待超时，请查看服务窗口中的日志。")
        return 1

    PORT_FILE.write_text(str(port), encoding="ascii")
    print("[3/3] 启动完成！")
    print()
    _report(port)
    _open_panel(port)
    print()
    print("  首次使用：")
    print("    1. 管理面板登录（默认 admin / admin）")
    print("    2. 选择「国内版」或「国际版」板块")
    print("    3. 点「开始浏览器登录」导入你的 Tabbit 账号")
    print()
    print("  关闭本窗口不会停止服务；停止请双击「结束.bat」。")
    print("=" * 52)
    return 0


def cmd_stop() -> int:
    print("=" * 52)
    print("  Tabbit2API 停止中...")
    print("=" * 52)

    killed = 0
    pid = _read_pid()
    if pid:
        if _pid_alive(pid):
            print(f"[*] 正在停止服务进程 PID {pid} ...")
            if _kill_pid_tree(pid):
                killed += 1
        else:
            print(f"[i] PID 文件记录的进程 {pid} 已不存在。")
        PID_FILE.unlink(missing_ok=True)

    n = _kill_by_commandline()
    if n:
        print(f"[*] 已停止 {n} 个残留服务进程。")
        killed += n

    PORT_FILE.unlink(missing_ok=True)
    print()
    if killed:
        print("[OK] Tabbit2API 已停止。")
    else:
        print("[i] 没有发现正在运行的 Tabbit2API 服务。")
    print("=" * 52)
    return 0


def cmd_restart() -> int:
    cmd_stop()
    print()
    time.sleep(1)
    return cmd_start()


# ── 输出 ────────────────────────────────────────────────────────
def _report(port: int | None) -> None:
    port = port or START_PORT
    print(f"    管理面板 : http://127.0.0.1:{port}/admin")
    print(f"    OpenAI   : http://127.0.0.1:{port}/v1/chat/completions")
    print(f"    Claude   : http://127.0.0.1:{port}/v1/messages")


def _open_panel(port: int | None) -> None:
    url = f"http://127.0.0.1:{port or START_PORT}/admin"
    try:
        webbrowser.open(url)
        print(f"    已在浏览器打开面板：{url}")
    except Exception:
        print(f"    请手动打开面板：{url}")


COMMANDS = {"start": cmd_start, "stop": cmd_stop, "restart": cmd_restart}

if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"未知命令：{cmd}（可用：start / stop / restart）")
        sys.exit(2)
    sys.exit(fn())
