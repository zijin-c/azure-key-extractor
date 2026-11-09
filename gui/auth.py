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
from datetime import date, datetime
from functools import wraps

from flask import abort, redirect, request, session, url_for, jsonify

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

        -- 独立于用户可删除的历史，只存 key 指纹，不保存密钥明文。
        CREATE TABLE IF NOT EXISTS key_extractions (
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            product TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            extracted_at TEXT NOT NULL,
            first_extracted_at TEXT,
            time_inferred INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, product, key_hash)
        );

        CREATE TABLE IF NOT EXISTS successful_accounts (
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            email_hash TEXT NOT NULL,
            succeeded_at TEXT NOT NULL,
            first_succeeded_at TEXT,
            time_inferred INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, email_hash)
        );

        CREATE TABLE IF NOT EXISTS user_policies (
            user_id INTEGER PRIMARY KEY,
            require_proxy INTEGER NOT NULL DEFAULT 0 CHECK(require_proxy IN (0, 1)),
            max_concurrency INTEGER NOT NULL DEFAULT 20 CHECK(max_concurrency BETWEEN 1 AND 20)
        );
    """)
    # 旧账本仅保留最近时间，不能直接当作首次日期使用。
    for table, column in (("key_extractions", "first_extracted_at"),
                          ("successful_accounts", "first_succeeded_at")):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        if "time_inferred" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN time_inferred INTEGER NOT NULL DEFAULT 1")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_first ON {table}(user_id, {column})")
    conn.commit()

    # 一次性补入升级前仍保留的历史；标记和统计在同一事务中提交。
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'key_stats_backfilled_v1'").fetchone():
            rows = conn.execute("""
                SELECT h.user_id, u.username, h.keys, h.created_at
                FROM task_history h LEFT JOIN users u ON u.id = h.user_id
                ORDER BY h.id
            """)
            for row in rows:
                try:
                    keys = json.loads(row["keys"] or "{}")
                except (ValueError, TypeError):
                    continue
                _record_key_extractions(conn, row["user_id"], row["username"], keys, row["created_at"], inferred=True)
            conn.execute("INSERT INTO settings (key, value) VALUES ('key_stats_backfilled_v1', '1')")
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'account_stats_backfilled_v1'").fetchone():
            for row in conn.execute("""
                SELECT h.user_id, u.username, h.email, h.created_at
                FROM task_history h LEFT JOIN users u ON u.id = h.user_id
                WHERE h.success = 1 ORDER BY h.id
            """):
                _record_successful_account(conn, row["user_id"], row["username"], row["email"], row["created_at"], inferred=True)
            conn.execute("INSERT INTO settings (key, value) VALUES ('account_stats_backfilled_v1', '1')")
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'first_extraction_dates_v1'").fetchone():
            for row in conn.execute("""
                SELECT h.user_id, u.username, h.email, h.success, h.keys, h.created_at
                FROM task_history h LEFT JOIN users u ON u.id = h.user_id ORDER BY h.id
            """):
                try:
                    keys = json.loads(row["keys"] or "{}")
                except (ValueError, TypeError):
                    keys = {}
                _record_key_extractions(conn, row["user_id"], row["username"], keys, row["created_at"], inferred=True)
                if row["success"]:
                    _record_successful_account(conn, row["user_id"], row["username"], row["email"], row["created_at"], inferred=True)
            conn.execute("INSERT INTO settings (key, value) VALUES ('first_extraction_dates_v1', '1')")

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


def user_exists(user_id: int) -> bool:
    return bool(_get_db().execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_policy(user_id: int) -> dict | None:
    row = _get_db().execute("""
        SELECT COALESCE(p.require_proxy, 0) AS require_proxy,
               COALESCE(p.max_concurrency, 20) AS max_concurrency
        FROM users u LEFT JOIN user_policies p ON p.user_id = u.id WHERE u.id = ?
    """, (user_id,)).fetchone()
    return {"require_proxy": bool(row["require_proxy"]),
            "max_concurrency": row["max_concurrency"]} if row else None


def save_user_policy(user_id: int, require_proxy: bool, max_concurrency: int) -> bool:
    if type(require_proxy) is not bool or type(max_concurrency) is not int or not 1 <= max_concurrency <= 20:
        raise ValueError("强制代理必须为开关，并发上限必须为 1–20 的整数")
    conn = _get_db()
    with conn:
        cur = conn.execute("""
            INSERT INTO user_policies (user_id, require_proxy, max_concurrency)
            SELECT id, ?, ? FROM users WHERE id = ?
            ON CONFLICT(user_id) DO UPDATE SET require_proxy = excluded.require_proxy,
                                               max_concurrency = excluded.max_concurrency
        """, (int(require_proxy), max_concurrency, user_id))
    return cur.rowcount > 0


def list_users() -> list[dict]:
    """列出所有用户（管理员用）"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, username, is_admin, created_at, last_login FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    """删除普通用户和历史；累计提取统计独立保留。"""
    conn = _get_db()
    with conn:
        cur = conn.execute("DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,))
        if not cur.rowcount:
            return False
        conn.execute("DELETE FROM task_history WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_policies WHERE user_id = ?", (user_id,))
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
def _stats_timestamp(value):
    try:
        # 保留记录中的墙上时间；旧记录未携带服务器时区，不能猜测其 UTC 偏移。
        return datetime.fromisoformat(value).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    except (TypeError, ValueError):
        return None


def _record_successful_account(conn, user_id, username, email, succeeded_at, inferred=False):
    """同一用户成功过的邮箱只计一次，不保留邮箱明文。"""
    if not isinstance(email, str) or not email.strip():
        return
    email_hash = hashlib.sha256(email.strip().casefold().encode("utf-8")).hexdigest()
    conn.execute("""
        INSERT INTO successful_accounts
            (user_id, username, email_hash, succeeded_at, first_succeeded_at, time_inferred)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, email_hash) DO UPDATE SET
            succeeded_at = MAX(successful_accounts.succeeded_at, excluded.succeeded_at),
            first_succeeded_at = CASE
                WHEN successful_accounts.first_succeeded_at IS NULL AND successful_accounts.time_inferred = 1
                     AND excluded.time_inferred = 0 THEN NULL
                WHEN successful_accounts.first_succeeded_at IS NULL THEN excluded.first_succeeded_at
                WHEN excluded.first_succeeded_at IS NULL THEN successful_accounts.first_succeeded_at
                ELSE MIN(successful_accounts.first_succeeded_at, excluded.first_succeeded_at) END
    """, (user_id, username or f"用户 #{user_id}", email_hash, succeeded_at,
          _stats_timestamp(succeeded_at), int(inferred)))


def _record_key_extractions(conn, user_id, username, keys, extracted_at, inferred=False):
    """按用户、产品、key 去重；失败任务中的部分有效结果也计入。"""
    if not isinstance(keys, dict):
        return
    for product, key in keys.items():
        if not isinstance(key, str) or not key.strip():
            continue
        fingerprint = hashlib.sha256(key.strip().upper().encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO key_extractions
               (user_id, username, product, key_hash, extracted_at, first_extracted_at, time_inferred)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, product, key_hash) DO UPDATE SET
                   extracted_at = MAX(key_extractions.extracted_at, excluded.extracted_at),
                   first_extracted_at = CASE
                       WHEN key_extractions.first_extracted_at IS NULL AND key_extractions.time_inferred = 1
                            AND excluded.time_inferred = 0 THEN NULL
                       WHEN key_extractions.first_extracted_at IS NULL THEN excluded.first_extracted_at
                       WHEN excluded.first_extracted_at IS NULL THEN key_extractions.first_extracted_at
                       ELSE MIN(key_extractions.first_extracted_at, excluded.first_extracted_at) END""",
            (user_id, username or f"用户 #{user_id}", product, fingerprint, extracted_at,
             _stats_timestamp(extracted_at), int(inferred)),
        )


def get_admin_user_identity(user_id):
    conn = _get_db()
    row = conn.execute("SELECT id, username, is_admin, 0 AS deleted FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        row = conn.execute("""
            SELECT user_id AS id, username, 0 AS is_admin, 1 AS deleted
            FROM key_extractions WHERE user_id = ?
            UNION ALL SELECT user_id, username, 0, 1 FROM successful_accounts WHERE user_id = ? LIMIT 1
        """, (user_id, user_id)).fetchone()
    return dict(row) if row else None


def get_admin_user_details(user_id, start=None, end=None, group="day"):
    """按首次提取/首次成功统计新增量，不读取密码、TOTP 或 Key 明文。"""
    formats = {"hour": "strftime('%Y-%m-%d %H:00:00', {column})",
               "day": "substr({column}, 1, 10)",
               "week": "date({column}, '-6 days', 'weekday 1')",
               "month": "substr({column}, 1, 7)"}
    if group not in formats:
        raise ValueError("时间分组仅支持小时、天、周、月")
    for value in (start, end):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("日期格式必须为 YYYY-MM-DD") from None
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    conn = _get_db()
    conn.execute("SAVEPOINT admin_user_details")
    try:
        user = get_admin_user_identity(user_id)
        if user is None:
            return None

        periods, products, summary, lifetime, timing = {}, {}, {}, {}, {}
        for table, column, metric in (("key_extractions", "first_extracted_at", "new_keys"),
                                      ("successful_accounts", "first_succeeded_at", "new_successful_accounts")):
            total = conn.execute(f"""
                SELECT COUNT(*) AS total, COALESCE(SUM(time_inferred), 0) AS inferred,
                       COALESCE(SUM({column} IS NULL), 0) AS unknown
                FROM {table} WHERE user_id = ?
            """, (user_id,)).fetchone()
            lifetime[metric] = total["total"]
            timing[metric] = {"inferred": total["inferred"], "unknown": total["unknown"]}
            where, params = ["user_id = ?"], [user_id]
            if start:
                where.append(f"{column} >= ?")
                params.append(start + " 00:00:00")
            if end:
                where.append(f"{column} <= ?")
                params.append(end + " 23:59:59")
            product_sql = "product" if table == "key_extractions" else "NULL"
            bucket = formats[group].format(column=column)
            rows = conn.execute(f"""
                SELECT {bucket} AS period, {product_sql} AS product, COUNT(*) AS count
                FROM {table} WHERE {' AND '.join(where)} GROUP BY period, 2
            """, params).fetchall()
            summary[metric] = sum(row["count"] for row in rows)
            for row in rows:
                period = periods.setdefault(row["period"], {"period": row["period"], "new_keys": 0,
                                                           "new_successful_accounts": 0, "products": []})
                period[metric] += row["count"]
                if row["product"] is not None:
                    products[row["product"]] = products.get(row["product"], 0) + row["count"]
                    period["products"].append({"product": row["product"], "count": row["count"]})
        timeline = sorted(periods.values(), key=lambda item: (item["period"] is not None, item["period"] or ""), reverse=True)
        for period in timeline:
            period["products"].sort(key=lambda item: (-item["count"], item["product"]))
        return {"user": user, "range": {"start": start, "end": end, "group": group},
                "summary": summary, "lifetime": lifetime, "timing": timing,
                "periods": timeline[:500], "truncated": len(timeline) > 500,
                "products": [{"product": product, "count": count}
                             for product, count in sorted(products.items(), key=lambda item: (-item[1], item[0]))]}
    finally:
        conn.execute("RELEASE SAVEPOINT admin_user_details")


def get_admin_stats() -> dict:
    """累计提取数与当前历史数分开统计，已删除用户仍能追溯。"""
    conn = _get_db()
    rows = conn.execute("""
        WITH extracted AS (
            SELECT user_id, MIN(username) AS username, COUNT(*) AS extracted_keys,
                   MAX(extracted_at) AS last_extracted_at
            FROM key_extractions GROUP BY user_id
        ), history AS (
            SELECT user_id, COUNT(*) AS history_count FROM task_history GROUP BY user_id
        ), successes AS (
            SELECT user_id, MIN(username) AS username, COUNT(*) AS successful_accounts
            FROM successful_accounts GROUP BY user_id
        ), identities AS (
            SELECT id AS user_id FROM users UNION SELECT user_id FROM extracted
            UNION SELECT user_id FROM successes
        )
        SELECT i.user_id AS id, COALESCE(u.username, e.username, s.username) AS username,
               COALESCE(u.is_admin, 0) AS is_admin, u.created_at, u.last_login,
               u.id IS NULL AS deleted, COALESCE(e.extracted_keys, 0) AS extracted_keys,
               COALESCE(h.history_count, 0) AS history_count, e.last_extracted_at,
               COALESCE(s.successful_accounts, 0) AS successful_accounts,
               COALESCE(p.require_proxy, 0) AS require_proxy,
               COALESCE(p.max_concurrency, 20) AS max_concurrency
        FROM identities i LEFT JOIN users u ON u.id = i.user_id
        LEFT JOIN extracted e ON e.user_id = i.user_id
        LEFT JOIN history h ON h.user_id = i.user_id
        LEFT JOIN successes s ON s.user_id = i.user_id
        LEFT JOIN user_policies p ON p.user_id = i.user_id
        ORDER BY extracted_keys DESC, i.user_id
    """).fetchall()
    users = [dict(row) for row in rows]
    products = [dict(row) for row in conn.execute("""
        SELECT product, COUNT(*) AS extracted_keys FROM key_extractions
        GROUP BY product ORDER BY extracted_keys DESC, product
    """)]
    return {
        "users": users,
        "products": products,
        "summary": {
            "active_users": sum(not user["deleted"] for user in users),
            "users_with_keys": sum(user["extracted_keys"] > 0 for user in users),
            "extracted_keys": sum(user["extracted_keys"] for user in users),
            "successful_accounts": sum(user["successful_accounts"] for user in users),
        },
    }


def save_result(user_id: int, result_dict: dict):
    """历史和累计统计原子写入，删除历史不影响累计统计。"""
    conn = _get_db()
    keys = result_dict.get("keys", {})
    created_at = result_dict.get("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with conn:
        user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        _record_key_extractions(conn, user_id, user["username"] if user else None, keys, created_at)
        if result_dict.get("success"):
            _record_successful_account(conn, user_id, user["username"] if user else None,
                                       result_dict.get("email"), created_at)
        conn.execute(
            """INSERT INTO task_history (user_id, email, password, totp_secret, success, keys, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                result_dict.get("email", ""),
                result_dict.get("password", ""),
                result_dict.get("totp_secret", ""),
                1 if result_dict.get("success") else 0,
                json.dumps(keys, ensure_ascii=False),
                result_dict.get("msg", ""),
                created_at,
            ),
        )


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


def admin_required(f):
    """每次请求从数据库校验权限，不能依赖前端隐藏或会话中的角色。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.endpoint in ("admin_page", "admin_user_page"):
                return redirect(url_for("login_page"))
            return jsonify({"ok": False, "error": "未登录", "need_login": True}), 401
        if not is_user_admin(session["user_id"]):
            if request.endpoint in ("admin_page", "admin_user_page"):
                abort(403)
            return jsonify({"ok": False, "error": "仅管理员可访问后台"}), 403
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
