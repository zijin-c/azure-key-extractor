"""
批次任务管理器与服务自动重启控制
------------------------------------
核心机制:
1. 账号按批次执行 (默认 15 个为一批次)。
2. 每完成一批次，将未处理账号与已完成结果持久化至本地 JSON 文件，
   随后触发程序强制重启（等同于在服务器上执行 `systemctl restart azure-key`），彻底杀死残留浏览器和驱动，释放全部系统内存。
3. 重启后服务自动唤醒并检测待处理任务，无缝继续执行下一批次。
4. 全部批次完成时，自动汇总导出完整 Excel，并执行最终强制重启以彻底清理残留。
5. 最终重启后处于完全就绪的空闲状态，杜绝任何死循环。
"""

import os
import sys
import json
import time
import math
import logging
import subprocess
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

from modules.pipeline import Account, KeyResult
from utils.process_manager import BrowserProcessManager

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_QUEUE_FILE = os.path.join(_DATA_DIR, "task_queue.json")


def _account_to_dict(a: Account) -> dict:
    return {
        "email": a.email,
        "password": a.password,
        "totp_secret": getattr(a, "totp_secret", "") or ""
    }


def _dict_to_account(d: dict) -> Account:
    return Account(
        email=d.get("email", ""),
        password=d.get("password", ""),
        totp_secret=d.get("totp_secret", "") or ""
    )


def _result_to_dict(r: KeyResult) -> dict:
    return {
        "email": r.account.email,
        "password": r.account.password,
        "totp_secret": r.totp_secret or "",
        "success": bool(r.success),
        "keys": r.keys or {},
        "message": r.message or "",
        "ts": r.ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def _dict_to_result(d: dict) -> KeyResult:
    return KeyResult(
        account=_dict_to_account(d),
        success=bool(d.get("success", False)),
        totp_secret=d.get("totp_secret", "") or "",
        keys=d.get("keys", {}) or {},
        message=d.get("message", d.get("msg", "")) or "",
        ts=d.get("ts", "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


class BatchManager:
    """管理多批次任务持久化与服务自动重启调度器。"""

    @classmethod
    def _save_task_data(cls, data: dict):
        """原子写入任务队列文件，避免突然被终止时数据损坏。"""
        tmp_file = _QUEUE_FILE + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if os.path.exists(_QUEUE_FILE):
                os.replace(tmp_file, _QUEUE_FILE)
            else:
                os.rename(tmp_file, _QUEUE_FILE)
        except Exception as e:
            log.error(f"保存任务状态文件失败: {e}")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass

    @classmethod
    def get_active_task(cls) -> Optional[dict]:
        """读取当前正在进行的任务配置与状态。"""
        if not os.path.exists(_QUEUE_FILE):
            return None
        try:
            with open(_QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"读取任务状态文件失败: {e}")
            return None

    @classmethod
    def init_task(
        cls,
        sid: str,
        user_id: Optional[int],
        accounts: List[Account],
        headless: bool = False,
        proxy: Optional[str] = None,
        vless_proxy: Optional[str] = None,
        selected_products: Optional[list] = None,
        concurrency: int = 3,
        mode: str = "azure_student",
        batch_size: int = 0,
    ) -> dict:
        """初始化任务并持久化。"""
        total_accounts = len(accounts)
        if batch_size and int(batch_size) > 0:
            batch_size = int(batch_size)
            total_batches = math.ceil(total_accounts / batch_size) if total_accounts > 0 else 1
        else:
            batch_size = total_accounts if total_accounts > 0 else 1
            total_batches = 1

        task_data = {
            "task_id": sid,
            "sid": sid,
            "user_id": user_id,
            "headless": headless,
            "proxy": proxy,
            "vless_proxy": vless_proxy,
            "selected_products": selected_products,
            "concurrency": concurrency,
            "mode": mode,
            "batch_size": batch_size,
            "current_batch_index": 0,
            "total_batches": total_batches,
            "total_accounts": total_accounts,
            "status": "running",  # running | batch_restarting | all_done_restarting | stopped | completed
            "all_accounts": [_account_to_dict(a) for a in accounts],
            "completed_results": [],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        cls._save_task_data(task_data)
        return task_data

    @classmethod
    def get_current_batch(cls) -> Tuple[List[Account], int, int, int, int]:
        """
        获取当前批次的账号列表及进度信息。
        返回: (batch_accounts, current_batch_index, total_batches, offset, total_accounts)
        """
        task = cls.get_active_task()
        if not task:
            return [], 0, 1, 0, 0

        accounts_data = task.get("all_accounts", [])
        total = len(accounts_data)
        batch_size = max(1, task.get("batch_size", total or 1))
        batch_idx = task.get("current_batch_index", 0)
        total_batches = task.get("total_batches", 1)

        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, total)

        batch_raw = accounts_data[start_idx:end_idx]
        batch_accounts = [_dict_to_account(d) for d in batch_raw]
        return batch_accounts, batch_idx, total_batches, start_idx, total

    @classmethod
    def record_account_result(cls, result: KeyResult):
        """记录单个账号的处理结果（用于跨批次/跨重启汇总）。"""
        task = cls.get_active_task()
        if not task:
            return
        results = task.setdefault("completed_results", [])
        results.append(_result_to_dict(result))
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._save_task_data(task)

    @classmethod
    def has_next_batch(cls) -> bool:
        """判断是否还有下一批次待执行。"""
        task = cls.get_active_task()
        if not task:
            return False
        curr = task.get("current_batch_index", 0)
        total_b = task.get("total_batches", 1)
        return (curr + 1) < total_b

    @classmethod
    def advance_to_next_batch(cls, restarting: bool = False):
        """推进批次索引，并更新状态。"""
        task = cls.get_active_task()
        if not task:
            return
        task["current_batch_index"] = task.get("current_batch_index", 0) + 1
        task["status"] = "batch_restarting" if restarting else "running"
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._save_task_data(task)

    @classmethod
    def mark_all_done(cls):
        """标记所有批次已跑完，准备执行最终重启。"""
        task = cls.get_active_task()
        if not task:
            return
        task["status"] = "all_done_restarting"
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._save_task_data(task)

    @classmethod
    def stop_task(cls):
        """用户中止任务：清理/置停任务状态。"""
        task = cls.get_active_task()
        if task:
            task["status"] = "stopped"
            task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cls._save_task_data(task)
        cls.clear_task()

    @classmethod
    def clear_task(cls):
        """彻底移除任务文件。"""
        if os.path.exists(_QUEUE_FILE):
            try:
                os.remove(_QUEUE_FILE)
            except Exception as e:
                log.warning(f"清除任务状态文件失败: {e}")

    @classmethod
    def is_running(cls) -> bool:
        """检查是否有正在运行或在批次重启过渡中的任务。"""
        task = cls.get_active_task()
        if not task:
            return False
        return task.get("status") in ("running", "batch_restarting")

    @classmethod
    def get_completed_count(cls) -> int:
        """获取已完成账号数量。"""
        task = cls.get_active_task()
        if not task:
            return 0
        return len(task.get("completed_results", []))

    @classmethod
    def get_all_results(cls) -> List[KeyResult]:
        """获取所有批次已累积的完整 KeyResult 列表。"""
        task = cls.get_active_task()
        if not task:
            return []
        raw_list = task.get("completed_results", [])
        return [_dict_to_result(d) for d in raw_list]

    @classmethod
    def trigger_service_restart(cls, delay: float = 1.5):
        """
        触发服务强制重启，效果等同于在服务器上执行 `systemctl restart azure-key`。
        1. 杀死所有残留的浏览器与 Playwright 驱动。
        2. 等待 delay 秒（确保 SSE / HTTP 响应顺利回传给前端网页）。
        3. 若部署在 Linux systemd 下，直接调用 systemctl restart --no-block azure-key；
           若有守护进程 (Restart=always)，退出进程由 systemd 重新拉起；
           若在普通终端或 Windows 下，启动全新 Python 实例后退出当前进程。
        """
        log.info("准备执行服务强制重启...")
        try:
            BrowserProcessManager.cleanup_all()
        except Exception as e:
            log.warning(f"清理残留进程时出错: {e}")

        if delay > 0:
            time.sleep(delay)

        # Linux 环境处理
        if sys.platform != "win32":
            # 策略 A: 尝试通过 systemctl 重启 azure-key 服务
            restarted_via_systemctl = False
            for cmd in [
                ["systemctl", "restart", "--no-block", "azure-key"],
                ["sudo", "systemctl", "restart", "--no-block", "azure-key"]
            ]:
                try:
                    res = subprocess.run(cmd, capture_output=True, timeout=3)
                    if res.returncode == 0:
                        log.info(f"✅ 成功执行命令: {' '.join(cmd)}")
                        restarted_via_systemctl = True
                        break
                except Exception:
                    pass

            if restarted_via_systemctl:
                time.sleep(0.5)
                os._exit(0)

            # 策略 B: 检查是否在 systemd 服务守护中 (azure-key.service 有 Restart=always)
            if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
                log.info("检测到处于 systemd 守护进程中 (Restart=always)，退出当前进程交由 systemd 自动拉起...")
                os._exit(0)

            # 策略 C: 独立终端运行模式，延迟 1.5 秒等端口 5010 释放后启动新进程
            log.info("独立 Linux 终端运行模式，通过新 Python 进程重新启动...")
            entry = os.path.join(_BASE_DIR, "run_gui.py")
            cmd = [sys.executable, entry]
            script = f"""import time, subprocess, sys
time.sleep(1.5)
subprocess.Popen({repr(cmd)}, cwd={repr(_BASE_DIR)})
"""
            subprocess.Popen([sys.executable, "-c", script], cwd=_BASE_DIR)
            os._exit(0)
        else:
            # Windows 环境处理
            log.info("Windows 运行模式，延迟 1.5 秒后启动全新 Python 进程重新拉起...")
            entry = os.path.join(_BASE_DIR, "run_gui.py")
            cmd = [sys.executable, entry]
            script = f"""import time, subprocess, sys
time.sleep(1.5)
subprocess.Popen({repr(cmd)}, cwd={repr(_BASE_DIR)})
"""
            subprocess.Popen([sys.executable, "-c", script], cwd=_BASE_DIR)
            os._exit(0)
