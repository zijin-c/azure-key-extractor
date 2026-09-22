"""
用户认证模块 - SQLite 存储
支持注册、登录、会话管理、历史记录持久化
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime
from functools import wraps

from flask import redirect, request, session, url_for, jsonify

# ── 数据库路径 ─────────────────────────────────────────────────
_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_DB_DIR, exist_ok=True)
DB_PATH = os.path.join(_DB_DIR, "users.db")

_local = threading.local()


def _get_db() -> sqlite3.Connection:
    """每个线程一个连接"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def init_db():
    """初始化数据库表"""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            password TEXT NOT NULL,
            totp_secret TEXT DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            keys TEXT DEFAULT '{}',
            message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_history_user ON task_history(user_id);
        CREATE INDEX IF NOT EXISTS idx_history_time ON task_history(created_at DESC);
    """)
    conn.commit()

    # 确保有默认管理员（首次启动时创建）
    admin = conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchone()
    if not admin:
        salt = secrets.token_hex(16)
        pw_hash = _hash_password("admin123", salt)
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, salt, is_admin, created_at) VALUES (?, ?, ?, 1, ?)",
            ("admin", pw_hash, salt, now),
        )
        conn.commit()
        print("[OK] 默认管理员已创建: admin / admin123（请尽快修改密码）")



# ── 密码哈希 ─────────────────────────────────────────────────
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    ).hex()


# ── 用户操作 ─────────────────────────────────────────────────
def create_user(username: str, password: str, is_admin: bool = False) -> tuple[bool, str]:
    """创建用户，返回 (成功, 消息)"""
    username = username.strip()
    if not username or not password:
        return False, "用户名和密码不能为空"
    if len(username) < 2 or len(username) > 32:
        return False, "用户名长度 2-32 字符"
    if len(password) < 4:
        return False, "密码至少 4 位"

    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    now = datetime.now().isoformat()

    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, pw_hash, salt, 1 if is_admin else 0, now),
        )
        conn.commit()
        return True, "注册成功"
    except sqlite3.IntegrityError:
        return False, "用户名已存在"


def verify_user(username: str, password: str) -> tuple[bool, int | None]:
    """验证用户，返回 (成功, user_id)"""
    conn = _get_db()
    row = conn.execute(
        "SELECT id, password_hash, salt FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not row:
        return False, None

    pw_hash = _hash_password(password, row["salt"])
    if pw_hash != row["password_hash"]:
        return False, None

    # 更新最后登录时间
    conn.execute(
        "UPDATE users SET last_login = ? WHERE id = ?",
        (datetime.now().isoformat(), row["id"]),
    )
    conn.commit()
    return True, row["id"]


def is_user_admin(user_id: int) -> bool:
    """检查用户是否是管理员"""
    conn = _get_db()
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def list_users() -> list[dict]:
    """列出所有用户（管理员用）"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, username, is_admin, created_at, last_login FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    """删除用户及其历史记录"""
    conn = _get_db()
    conn.execute("DELETE FROM task_history WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,))
    conn.commit()
    return True


def change_password(user_id: int, new_password: str) -> bool:
    """修改密码"""
    if len(new_password) < 4:
        return False
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(new_password, salt)
    conn = _get_db()
    conn.execute(
        "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
        (pw_hash, salt, user_id),
    )
    conn.commit()
    return True


# ── 历史记录 ─────────────────────────────────────────────────
def save_result(user_id: int, result_dict: dict):
    """保存单条任务结果到数据库"""
    conn = _get_db()
    conn.execute(
        """INSERT INTO task_history (user_id, email, password, totp_secret, success, keys, message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            result_dict.get("email", ""),
            result_dict.get("password", ""),
            result_dict.get("totp_secret", ""),
            1 if result_dict.get("success") else 0,
            json.dumps(result_dict.get("keys", {}), ensure_ascii=False),
            result_dict.get("msg", ""),
            result_dict.get("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ),
    )
    conn.commit()


def get_history(user_id: int, limit: int = 500) -> list[dict]:
    """获取用户历史记录"""
    conn = _get_db()
    rows = conn.execute(
        """SELECT id, email, password, totp_secret, success, keys, message, created_at
           FROM task_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "email": r["email"],
            "password": r["password"],
            "totp_secret": r["totp_secret"],
            "success": bool(r["success"]),
            "keys": json.loads(r["keys"]) if r["keys"] else {},
            "msg": r["message"],
            "ts": r["created_at"],
        })
    return results


def delete_history_items(user_id: int, item_ids: list[int]) -> int:
    """删除指定历史记录，返回删除数量"""
    if not item_ids:
        return 0
    conn = _get_db()
    placeholders = ",".join("?" * len(item_ids))
    cur = conn.execute(
        f"DELETE FROM task_history WHERE user_id = ? AND id IN ({placeholders})",
        [user_id] + item_ids,
    )
    conn.commit()
    return cur.rowcount


def clear_history(user_id: int) -> int:
    """清空用户所有历史"""
    conn = _get_db()
    cur = conn.execute("DELETE FROM task_history WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


# ── Flask 装饰器 ─────────────────────────────────────────────
def login_required(f):
    """路由保护装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            # API 请求返回 401，页面请求重定向
            if request.path.startswith("/api/") or request.path == "/stream":
                return jsonify({"ok": False, "error": "未登录", "need_login": True}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ── 用户专属设置 ─────────────────────────────────────────────
def save_user_setting(user_id: int, key: str, value: str):
    """保存用户的专属设置"""
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
        (user_id, key, value),
    )
    conn.commit()


def get_user_settings(user_id: int) -> dict[str, str]:
    """获取用户的所有专属设置"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT key, value FROM user_settings WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}
