"""
浏览器与 Playwright 驱动进程生命周期管理模块
负责在任务执行中精确记录进程树，并在单账号结束、批量任务结束、任务中止等各种情况下
100% 彻底杀死所有拉起的浏览器（Chromium / Chrome / Edge）及 Playwright Driver，释放全部系统内存，
同时杜绝误杀用户平时正常运行的个人浏览器。
"""
import os
import sys
import time
import signal
import asyncio
import subprocess
import threading
from typing import Set, Dict, List, Optional

# Windows API 结构体定义（基于 ctypes，毫秒级执行，零第三方依赖）
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]


def get_playwright_driver_pid(pw) -> Optional[int]:
    """从 Playwright 实例中提取底层 Node/Driver 驱动进程的 PID。"""
    try:
        impl = getattr(pw, "_impl_obj", pw)
        conn = getattr(impl, "_connection", None)
        if conn:
            transport = getattr(conn, "_transport", None)
            if transport:
                proc = getattr(transport, "_proc", None)
                if proc and hasattr(proc, "pid"):
                    return int(proc.pid)
    except Exception:
        pass
    return None


class BrowserProcessManager:
    """全局进程生命周期管理器（线程安全）。"""
    _lock = threading.Lock()
    _active_driver_pids: Set[int] = set()
    _tracked_child_pids: Dict[int, Set[int]] = {}

    @classmethod
    def register_driver(cls, driver_pid: Optional[int]):
        """登记新启动的 Playwright Driver 进程 PID。"""
        if not driver_pid or driver_pid <= 0:
            return
        with cls._lock:
            cls._active_driver_pids.add(driver_pid)
            cls._tracked_child_pids.setdefault(driver_pid, set())

    @classmethod
    def record_descendants(cls, driver_pid: Optional[int]) -> Set[int]:
        """快照并记录指定 Driver PID 当前的所有子孙进程（浏览器主进程、渲染进程、GPU进程等）。"""
        if not driver_pid or driver_pid <= 0:
            return set()
        descendants = cls._get_descendant_pids([driver_pid])
        with cls._lock:
            if driver_pid in cls._tracked_child_pids:
                cls._tracked_child_pids[driver_pid].update(descendants)
            else:
                cls._tracked_child_pids[driver_pid] = set(descendants)
        return descendants

    @classmethod
    def kill_driver_and_descendants(cls, driver_pid: Optional[int]) -> int:
        """强制杀死指定 Driver 及其派生的所有浏览器子进程树，并注销登记。"""
        if not driver_pid or driver_pid <= 0:
            return 0

        with cls._lock:
            recorded_children = cls._tracked_child_pids.pop(driver_pid, set())
            cls._active_driver_pids.discard(driver_pid)

        # 再次获取当前可能残留的实时子进程树
        live_descendants = cls._get_descendant_pids([driver_pid])
        all_pids_to_kill = {driver_pid} | recorded_children | live_descendants

        killed_count = cls._kill_pids(all_pids_to_kill)
        return killed_count

    @classmethod
    def cleanup_all(cls) -> int:
        """全量清理：杀死所有登记过的 Driver 及其子进程，并针对专属参数进行孤儿进程二次扫描兜底。"""
        with cls._lock:
            drivers = set(cls._active_driver_pids)
            all_known = set(drivers)
            for c_set in cls._tracked_child_pids.values():
                all_known.update(c_set)
            cls._active_driver_pids.clear()
            cls._tracked_child_pids.clear()

        # 1. 终止所有登记的进程及其子树
        if drivers:
            live_descendants = cls._get_descendant_pids(list(drivers))
            all_known.update(live_descendants)

        killed = cls._kill_pids(all_known)

        # 2. 针对本工具专属特征（如 shared_cache 路径、专用指纹参数）进行孤儿浏览器进程兜底清理
        orphans = cls._find_orphan_browsers()
        if orphans:
            killed += cls._kill_pids(orphans)

        # 3. 强制触发 Python 垃圾回收与 glibc malloc_trim 归还系统内存
        release_system_memory()

        return killed

    @classmethod
    def _get_descendant_pids(cls, root_pids: List[int]) -> Set[int]:
        """获取给定根 PID 列表的所有递归子孙进程 PID。"""
        if not root_pids:
            return set()

        root_set = set(root_pids)
        children_map: Dict[int, List[int]] = {}

        if sys.platform == "win32":
            try:
                kernel32 = ctypes.windll.kernel32
                hSnapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
                if hSnapshot != -1 and hSnapshot != 0:
                    pe32 = PROCESSENTRY32()
                    pe32.dwSize = ctypes.sizeof(PROCESSENTRY32)
                    if kernel32.Process32First(hSnapshot, ctypes.byref(pe32)):
                        while True:
                            ppid = pe32.th32ParentProcessID
                            pid = pe32.th32ProcessID
                            children_map.setdefault(ppid, []).append(pid)
                            if not kernel32.Process32Next(hSnapshot, ctypes.byref(pe32)):
                                break
                    kernel32.CloseHandle(hSnapshot)
            except Exception:
                pass
        else:
            # Linux / macOS
            try:
                out = subprocess.check_output(["ps", "-A", "-o", "pid,ppid"], text=True)
                for line in out.strip().splitlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                        pid, ppid = int(parts[0]), int(parts[1])
                        children_map.setdefault(ppid, []).append(pid)
            except Exception:
                pass

        descendants: Set[int] = set()
        stack = list(root_set)
        while stack:
            curr = stack.pop()
            for child in children_map.get(curr, []):
                if child not in descendants and child not in root_set:
                    descendants.add(child)
                    stack.append(child)

        return descendants

    @classmethod
    def _kill_pids(cls, pids: Set[int]) -> int:
        """强制杀死一组 PID。"""
        if not pids:
            return 0

        current_pid = os.getpid()
        killed = 0

        for pid in list(pids):
            if pid <= 0 or pid == current_pid:
                continue

            try:
                if sys.platform == "win32":
                    # 使用 Windows taskkill /T /F 强制连带杀死进程树
                    res = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                    )
                    if res.returncode == 0:
                        killed += 1
                else:
                    # Linux / macOS
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                    except OSError:
                        pass
            except Exception:
                pass

        return killed

    @classmethod
    def _find_orphan_browsers(cls) -> Set[int]:
        """
        检索由于异常崩溃或脱壳可能遗留的孤儿浏览器/驱动/代理进程。
        仅匹配包含本项目专属特征参数的进程，100% 避免误伤用户自身的正常浏览器。
        """
        orphans: Set[int] = set()
        current_pid = os.getpid()

        if sys.platform == "win32":
            try:
                # 仅查询包含 shared_cache、AutomationControlled 或专用指纹参数的 edge/chrome/chromium/node/xray 进程
                ps_cmd = (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { ($_.Name -match 'msedge|chrome|chromium|node|xray') -and "
                    "($_.CommandLine -match 'shared_cache|AutomationControlled|force-webrtc-ip-handling-policy|ms-playwright|xray_chain_|playwright/driver|xray run') } | "
                    "Select-Object -ExpandProperty ProcessId"
                )
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pid = int(line)
                        if pid != current_pid:
                            orphans.add(pid)
            except Exception:
                pass
        else:
            try:
                # 针对 Linux 环境检索可能遗留的本项目专有 Chromium / Node 驱动 / Xray 孤儿进程
                res = subprocess.run(
                    ["pgrep", "-a", "-f", "AutomationControlled|force-webrtc-ip-handling-policy|ms-playwright|xray_chain_|playwright/driver|xray run"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                for line in res.stdout.splitlines():
                    parts = line.strip().split(maxsplit=1)
                    if parts and parts[0].isdigit():
                        pid = int(parts[0])
                        if pid != current_pid:
                            orphans.add(pid)
            except Exception:
                pass

        return orphans


def release_system_memory():
    """
    强制执行 Python 全局垃圾回收，并通过 glibc 的 malloc_trim(0)
    将未使用的堆内存页强制归还给 Linux 内核，彻底解决长期运行服务的内存空挂/碎片化问题。
    """
    try:
        import gc
        gc.collect()
        if sys.platform != "win32":
            try:
                import ctypes
                libc = ctypes.CDLL("libc.so.6")
                if hasattr(libc, "malloc_trim"):
                    libc.malloc_trim(0)
            except Exception:
                pass
    except Exception:
        pass


async def safe_close_playwright(
    browser=None,
    ctx=None,
    pw=None,
    driver_pid: Optional[int] = None,
    timeout: float = 3.0,
):
    """
    异步安全清理 Playwright 资源：
    1. 使用 asyncio.shield 包装关闭调用，防止任务因被 cancel 导致关闭逻辑被腰斩；
    2. 设置超时机制，防止 CDP 连接卡死；
    3. 最后无条件执行底层进程树强制杀死，保证 100% 进程与内存释放；
    4. 主动触发垃圾回收与 glibc malloc_trim 归还系统内存。
    """
    # 1. 尝试优雅关闭 context
    if ctx:
        try:
            await asyncio.shield(asyncio.wait_for(ctx.close(), timeout=timeout))
        except BaseException:
            pass

    # 2. 尝试优雅关闭 browser
    if browser:
        try:
            await asyncio.shield(asyncio.wait_for(browser.close(), timeout=timeout))
        except BaseException:
            pass

    # 3. 尝试优雅停止 playwright driver
    if pw:
        try:
            await asyncio.shield(asyncio.wait_for(pw.stop(), timeout=timeout))
        except BaseException:
            pass

    # 4. 底层无条件强制杀死该驱动对应的整棵进程树（浏览器、GPU、渲染器、驱动）
    if driver_pid:
        try:
            BrowserProcessManager.kill_driver_and_descendants(driver_pid)
        except Exception:
            pass

    # 5. 触发垃圾回收与 glibc 内存修剪归还内核
    release_system_memory()
