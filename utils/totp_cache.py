"""
2FA TOTP 密钥持久化磁盘缓存模块
----------------------------------------
确保任何账号在任何阶段（网络响应拦截、DOM提取、MFA绑定中途）一旦生成 2FA Secret，
立即毫秒级写入本地持久化缓存，防止因网络重试、浏览器关闭、服务重启导致 2FA 密钥丢失。
"""

import os
import re
import json
import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
_CACHE_FILE = os.path.join(_DATA_DIR, "totp_cache.json")
_LOCK = threading.Lock()

_MEMORY_CACHE: dict[str, str] = {}
_INITIALIZED = False


def _is_valid_secret(candidate: str | None) -> bool:
    if not candidate:
        return False
    s = re.sub(r"[\s-]+", "", candidate.strip().upper())
    if len(s) < 16 or len(s) > 64:
        return False
    return bool(re.match(r"^[A-Z2-7]+$", s))


def _ensure_loaded():
    global _INITIALIZED, _MEMORY_CACHE
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        os.makedirs(_DATA_DIR, exist_ok=True)
        if os.path.isfile(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        _MEMORY_CACHE = {k.lower().strip(): v.strip() for k, v in data.items() if _is_valid_secret(v)}
            except Exception as e:
                log.warning(f"[TOTP Cache] 读取缓存文件失败: {e}")

        # 尝试从历史 results/*.jsonl 中回溯已存在的 2FA 密钥补充缓存
        results_dir = os.path.join(_BASE_DIR, "results")
        if os.path.isdir(results_dir):
            try:
                for fname in os.listdir(results_dir):
                    if fname.startswith("keys_") and fname.endswith(".jsonl"):
                        fpath = os.path.join(results_dir, fname)
                        with open(fpath, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    item = json.loads(line)
                                    em = (item.get("email") or "").lower().strip()
                                    sec = (item.get("totp_secret") or "").strip()
                                    if em and _is_valid_secret(sec) and em not in _MEMORY_CACHE:
                                        _MEMORY_CACHE[em] = sec
                                except Exception:
                                    pass
            except Exception:
                pass

        _INITIALIZED = True


def save_totp_cache(email: str, secret: str) -> bool:
    """持久化保存账号的 TOTP 密钥到本地文件与内存缓存。"""
    if not email or not secret:
        return False
    clean_email = email.lower().strip()
    clean_secret = re.sub(r"[\s-]+", "", secret.strip().upper())
    if not _is_valid_secret(clean_secret):
        return False

    _ensure_loaded()
    with _LOCK:
        if _MEMORY_CACHE.get(clean_email) == clean_secret:
            return True
        _MEMORY_CACHE[clean_email] = clean_secret
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(_MEMORY_CACHE, f, ensure_ascii=False, indent=2)
            log.info(f"[TOTP Cache] 已持久化保存账号 {clean_email} 2FA密钥: {clean_secret[:4]}***")
            return True
        except Exception as e:
            log.warning(f"[TOTP Cache] 写入缓存文件失败: {e}")
            return False


def get_cached_totp(email: str) -> str:
    """查询本地缓存中是否已存在该账号的历史 2FA 密钥。"""
    if not email:
        return ""
    _ensure_loaded()
    clean_email = email.lower().strip()
    with _LOCK:
        return _MEMORY_CACHE.get(clean_email, "")
