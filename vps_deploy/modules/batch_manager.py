"""按用户持久化网页任务与批次进度。

网页请求和任务线程通过 task_scope 选择 data/tasks/user_<id>.json。
CLI 保留旧单文件路径。网页批次只做用户范围清理，不重启整个服务。
"""

import os
import sys
import json
import time
import math
import logging
import subprocess
import tempfile
import threading
from functools import wraps
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

from modules.pipeline import Account, KeyResult
from utils.process_manager import BrowserProcessManager
from utils.task_scope import current_task_user, task_scope

log = logging.getLogger(__name__)
_QUEUE_LOCK = threading.RLock()


def _locked_queue(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _QUEUE_LOCK:
            return function(*args, **kwargs)
    return locked


def _account_identity(data):
    return (data.get("email", "").strip().lower(), data.get("password", ""))


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
    """管理用户队列持久化、批次推进与旧队列迁移。"""

    @classmethod
    def _queue_path(cls):
        user_id = current_task_user()
        if user_id is None:
            return _QUEUE_FILE
        return os.path.join(os.path.dirname(_QUEUE_FILE), "tasks", f"user_{user_id}.json")

    @classmethod
    @_locked_queue
    def migrate_legacy_task(cls):
        """升级时迁移旧单队列，不覆盖已经存在的用户队列。"""
        if not os.path.isfile(_QUEUE_FILE):
            return
        with open(_QUEUE_FILE, encoding="utf-8") as file:
            task = json.load(file)
        user_id = task.get("user_id")
        if type(user_id) is not int or user_id <= 0 or task.get("sid") != f"user_{user_id}":
            raise ValueError("旧任务缺少有效归属，无法自动恢复")
        with task_scope(user_id):
            if not os.path.exists(cls._queue_path()):
                cls._save_task_data(task)
        os.replace(_QUEUE_FILE, _QUEUE_FILE + ".legacy")

    @classmethod
    @_locked_queue
    def list_user_tasks(cls):
        root = os.path.join(os.path.dirname(_QUEUE_FILE), "tasks")
        if not os.path.isdir(root):
            return []
        tasks = []
        for name in os.listdir(root):
            if not name.startswith("user_") or not name.endswith(".json"):
                continue
            try:
                user_id = int(name[5:-5])
                with task_scope(user_id):
                    task = cls.get_active_task()
                if task and task.get("user_id") == user_id and task.get("sid") == f"user_{user_id}":
                    tasks.append(task)
            except (ValueError, OSError):
                log.exception("无法读取用户队列 %s", name)
        return tasks

    @classmethod
    @_locked_queue
    def _save_task_data(cls, data: dict):
        """原子写入任务队列文件，避免突然被终止时数据损坏。"""
        queue_path = cls._queue_path()
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        fd, tmp_file = tempfile.mkstemp(dir=os.path.dirname(queue_path), prefix=".task-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, queue_path)
        except Exception:
            log.exception("保存任务状态文件失败")
            raise
        finally:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

    @classmethod
    @_locked_queue
    def get_active_task(cls) -> Optional[dict]:
        """读取当前正在进行的任务配置与状态。"""
        queue_path = cls._queue_path()
        if not os.path.exists(queue_path):
            return None
        try:
            with open(queue_path, "r", encoding="utf-8") as f:
                task = json.load(f)
            user_id = current_task_user()
            if user_id is not None and (task.get("user_id") != user_id or task.get("sid") != f"user_{user_id}"):
                raise ValueError("队列归属不匹配")
            return task
        except Exception as e:
            log.warning(f"读取任务状态文件失败: {e}")
            return None

    @classmethod
    @_locked_queue
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
        owner = current_task_user()
        if owner is not None and (user_id != owner or sid != f"user_{owner}"):
            raise ValueError("不能向其他用户的队列写入任务")
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
    @_locked_queue
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

        completed = {_account_identity(d) for d in task.get("completed_results", [])}
        batch_raw = [d for d in accounts_data[start_idx:end_idx] if _account_identity(d) not in completed]
        batch_accounts = [_dict_to_account(d) for d in batch_raw]
        return batch_accounts, batch_idx, total_batches, start_idx, total

    @classmethod
    @_locked_queue
    def record_account_result(cls, result: KeyResult):
        """记录单个账号的处理结果（用于跨批次/跨重启汇总）。"""
        task = cls.get_active_task()
        if not task:
            return
        results = task.setdefault("completed_results", [])
        item = _result_to_dict(result)
        for i, existing in enumerate(results):
            if _account_identity(existing) == _account_identity(item):
                merged = dict(existing.get("keys", {}))
                merged.update({k: v for k, v in item["keys"].items() if v})
                item["keys"] = merged
                item["success"] = bool(any(merged.values())) or item["success"]
                results[i] = item
                break
        else:
            results.append(item)
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._save_task_data(task)

    @classmethod
    @_locked_queue
    def has_next_batch(cls) -> bool:
        """判断是否还有下一批次待执行。"""
        task = cls.get_active_task()
        if not task:
            return False
        curr = task.get("current_batch_index", 0)
        total_b = task.get("total_batches", 1)
        return (curr + 1) < total_b

    @classmethod
    @_locked_queue
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
    @_locked_queue
    def mark_all_done(cls, restarting: bool = True):
        """标记所有批次已跑完，准备执行最终重启。"""
        task = cls.get_active_task()
        if not task:
            return
        task["status"] = "all_done_restarting" if restarting else "completed"
        task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._save_task_data(task)

    @classmethod
    @_locked_queue
    def mark_failed(cls, sid: str):
        """只释放指定任务的运行标记，保留已完成结果供导出/排查。"""
        task = cls.get_active_task()
        if task and task.get("sid") == sid and task.get("status") in (
                "running", "batch_restarting", "all_done_restarting"):
            task["status"] = "failed"
            task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cls._save_task_data(task)

    @classmethod
    @_locked_queue
    def stop_task(cls):
        """用户中止任务：清理/置停任务状态。"""
        task = cls.get_active_task()
        if task:
            task["status"] = "stopped"
            task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cls._save_task_data(task)
        cls.clear_task()

    @classmethod
    @_locked_queue
    def clear_task(cls):
        """彻底移除任务文件。"""
        queue_path = cls._queue_path()
        if os.path.exists(queue_path):
            try:
                os.remove(queue_path)
            except Exception as e:
                log.warning(f"清除任务状态文件失败: {e}")

    @classmethod
    @_locked_queue
    def is_running(cls) -> bool:
        """检查是否有正在运行或在批次重启过渡中的任务。"""
        task = cls.get_active_task()
        if not task:
            return False
        return task.get("status") in ("running", "batch_restarting")

    @classmethod
    @_locked_queue
    def get_completed_count(cls) -> int:
        """获取已完成账号数量。"""
        task = cls.get_active_task()
        if not task:
            return 0
        return len(task.get("completed_results", []))

    @classmethod
    @_locked_queue
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
        if current_task_user() is not None:
            raise RuntimeError("用户任务不能重启整个服务，请使用用户范围清理")
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
