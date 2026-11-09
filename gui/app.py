"""
Azure Key 提取工具 - Web GUI 后端
Flask + SSE 实时日志 + 用户认证
"""
import asyncio
import json
import os
import queue
import re
import sys
import threading
import uuid
import concurrent.futures
from contextvars import copy_context
from functools import wraps
from urllib.parse import urlsplit
from dataclasses import dataclass, field

from datetime import datetime

import requests as _requests
from flask import Flask, Response, abort, g, jsonify, render_template, request, session, stream_with_context, send_file, redirect, url_for

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from modules.pipeline import Account, KeyResult, run_pipeline
from modules.batch_manager import BatchManager
from modules.excel_export import export_to_excel, PRODUCT_COLUMNS, PRODUCT_SHORT
from utils.xray_proxy import XrayProxyChain
from utils.process_manager import BrowserProcessManager, release_system_memory
from utils.session_secret import load_session_secret
from utils.totp_cache import totp_cache_scope, get_cached_totp, save_totp_cache
from utils.task_scope import current_task_user, task_scope, system_task_scope
from gui.auth import (
    init_db, create_user, verify_user, login_required, admin_required,
    save_result, get_history, delete_history_items, clear_history as db_clear_history,
    is_user_admin, list_users, delete_user, change_password,
    save_user_setting, get_user_settings, get_admin_stats,
    user_exists, get_user_policy, save_user_policy,
    get_admin_user_identity, get_admin_user_details,
    close_db,
)

if getattr(sys, 'frozen', False):
    _tmpl_dir = os.path.join(sys._MEIPASS, 'gui', 'templates')
else:
    _tmpl_dir = os.path.join(os.path.dirname(__file__), 'templates')

app = Flask(__name__, template_folder=_tmpl_dir)
app.secret_key = load_session_secret()
_task_start_lock = threading.Lock()
_maintenance_pending = False
_startup_recovery_pending = True

# 初始化数据库
with app.app_context():
    init_db()


def _startup_check_batch():
    """服务启动时检查是否有未完成的批次任务需要恢复，或刚完成所有任务后的最终清理。"""
    global _startup_recovery_pending
    import time
    time.sleep(2.5)  # 等待 Flask 监听端口与完全就绪
    try:
        try:
            BatchManager.migrate_legacy_task()
        except Exception:
            app.logger.exception("旧任务迁移失败，保留原文件供排查；继续恢复其他用户任务")
        for task_info in BatchManager.list_user_tasks():
            user_id = task_info["user_id"]
            sid = task_info["sid"]
            with task_scope(user_id):
                status = task_info.get("status")
                if status in ("running", "batch_restarting"):
                    with _task_start_lock:
                        s = _get_sess(sid)
                        if s.worker_active:
                            continue
                        s.running = s.worker_active = True
                        s.results = BatchManager.get_all_results()
                        _push_log(sid, "🚀 服务已重启，正在独立恢复当前用户的任务…")
                        try:
                            threading.Thread(target=_run_batch_worker, args=(sid,), daemon=True).start()
                        except Exception:
                            s.running = s.worker_active = False
                            BatchManager.mark_failed(sid)
                            app.logger.exception("恢复用户任务失败")
                elif status == "all_done_restarting":
                    _get_sess(sid).results = BatchManager.get_all_results()
                    BatchManager.mark_all_done(restarting=False)
                    _push_log(sid, "✅ 上次任务已完成，结果已恢复")

    except Exception as e:
        print(f"⚠️  启动恢复检查异常: {e}")
    finally:
        with _task_start_lock:
            _startup_recovery_pending = False


threading.Thread(target=_startup_check_batch, daemon=True).start()


def _periodic_memory_trim():
    """后台空闲内存回收守护线程：每隔 5 分钟在无任务运行时整理并释放内存给系统内核。"""
    import time
    while True:
        time.sleep(300)
        try:
            with _task_start_lock:
                idle = not _any_task_busy()
            if idle:
                release_system_memory()
        except Exception:
            pass


threading.Thread(target=_periodic_memory_trim, daemon=True).start()


# ── Session 状态 ───────────────────────────────────────────────
@dataclass
class _Sess:
    listeners:   list[queue.Queue] = field(default_factory=list)
    log_history: list = field(default_factory=list)
    running:     bool = False
    worker_active: bool = False
    results:     list = field(default_factory=list)
    task_loop:   asyncio.AbstractEventLoop | None = None
    task_ref:    asyncio.Task | None = None


_sessions: dict[str, _Sess] = {}
_sessions_lock = threading.Lock()


def _get_sess(sid: str) -> _Sess:
    with _sessions_lock:
        if sid not in _sessions:
            _sessions[sid] = _Sess()
        return _sessions[sid]


def _push_log(sid: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    item = {"ts": ts, "msg": msg}
    s = _get_sess(sid)
    # 保存进历史，最多只保留最近 100 条记录，防止长时间运行导致内存膨胀
    s.log_history.append(item)
    if len(s.log_history) > 100:
        s.log_history = s.log_history[-100:]
    # 分发广播给所有当前在线监听的设备/标签页
    for q in list(s.listeners):
        try:
            q.put(item)
        except Exception:
            pass


@app.before_request
def _ensure_sid():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if (request.headers.get("Sec-Fetch-Site") == "cross-site"
                or origin and urlsplit(origin).netloc.lower() != request.host.lower()):
            return jsonify({"ok": False, "error": "拒绝跨站操作请求"}), 403
    if "user_id" in session and not user_exists(session["user_id"]):
        session.clear()
        if request.endpoint not in {"login_page", "auth_login", "auth_logout"}:
            return jsonify({"ok": False, "error": "用户已删除，请重新登录", "need_login": True}), 401
    if "user_id" in session:
        session["sid"] = f"user_{session['user_id']}"
        # 旧标签页仍持有旧用户的数据，不能借切换后的浏览器 Cookie 写入新用户。
        expected_user = request.headers.get("X-User-ID")
        if expected_user is not None and expected_user != str(session["user_id"]):
            return jsonify({"ok": False, "error": "登录账号已切换，请刷新页面", "need_reload": True}), 409
        g.task_context = task_scope(session["user_id"])
        g.task_context.__enter__()
        if request.endpoint in {"api_status", "api_start", "api_stop", "api_restart", "api_cleanup", "api_results", "api_export", "stream"}:
            data = request.get_json(silent=True)
            supplied = request.args.getlist("sid")
            if isinstance(data, dict) and "sid" in data:
                supplied.append(data["sid"])
            if any(value is not None and value != "" and value != session["sid"] for value in supplied):
                return jsonify({"ok": False, "error": "不能访问其他用户的任务"}), 403
    elif "sid" not in session:
        session["sid"] = uuid.uuid4().hex


@app.after_request
def _private_response(response):
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.teardown_request
def _release_task_context(error):
    context = g.pop("task_context", None)
    if context is not None:
        context.__exit__(None, None, None)


def _validated_proxy_pool(proxy_input):
    """校验整个池，避免仅用非空文本绕过强制代理要求。"""
    if proxy_input is not None and not isinstance(proxy_input, str):
        raise ValueError("代理池必须为文本")
    proxies = config.parse_proxy_list(proxy_input)
    for index, proxy in enumerate(proxies, 1):
        try:
            parsed = urlsplit(proxy)
            if (parsed.scheme not in {"http", "https", "socks4", "socks5"}
                    or not parsed.hostname or not parsed.port
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                raise ValueError()
        except ValueError:
            raise ValueError(f"代理池第 {index} 个节点格式无效，请填写 host:port 或完整代理 URL") from None
    return proxies


def _requested_concurrency(value, maximum):
    if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d+", str(value).strip()):
        raise ValueError("并发数必须为整数")
    count = int(value)
    return max(1, min(count, maximum))


def _owns_task(task):
    return bool(task and task.get("user_id") == session.get("user_id") and task.get("sid") == session.get("sid"))


def _task_busy():
    task = BatchManager.get_active_task()
    sid = f"user_{current_task_user()}"
    current = _sessions.get(sid)
    return (_startup_recovery_pending or _maintenance_pending or bool(current and (current.running or current.worker_active))
            or bool(task and task.get("status") in ("running", "batch_restarting", "all_done_restarting")))


def _any_task_busy():
    return (any(s.running or s.worker_active for s in list(_sessions.values()))
            or any(task.get("status") in ("running", "batch_restarting", "all_done_restarting")
                   for task in BatchManager.list_user_tasks()))


def _task_status():
    """调用方持有启动锁；仅返回占用原因，不暴露其他用户的任务内容。"""
    sid = session["sid"]
    current = _get_sess(sid)
    task = BatchManager.get_active_task()
    owned = _owns_task(task)
    own_active = current.running or current.worker_active
    task_state = task.get("status") if task else None
    queue_busy = task_state in ("running", "batch_restarting", "all_done_restarting")
    busy = bool(_startup_recovery_pending or _maintenance_pending or own_active or queue_busy)
    can_stop = bool(owned and queue_busy and not _maintenance_pending
                    and not _startup_recovery_pending)
    if _startup_recovery_pending:
        state, message = "recovering", "服务正在检查并恢复任务，请稍候"
    elif _maintenance_pending:
        state, message = "restarting", "服务正在重启，请稍候"
    elif task_state == "all_done_restarting":
        state, message = "cleaning", "上次任务正在恢复收尾，请稍候，或停止旧任务后重新启动"
    elif task_state == "batch_restarting":
        state, message = "batch_restarting", "正在恢复当前用户的后续批次，请稍候"
    elif own_active:
        if not current.running or task_state not in ("running", "batch_restarting"):
            state, message = "cleaning", "任务正在收尾清理，完成后可启动"
        else:
            state, message = "running", "当前账号的任务正在运行"
    elif queue_busy:
        state, message = "recovery_required", "发现未恢复的任务记录；可停止旧任务后重新启动，或检查服务日志"
    elif owned and task_state == "failed":
        state, message = "failed", "上次任务异常退出，已释放占用；可重新启动，详情见日志"
    else:
        state, message = "idle", "就绪"
    return {"running": bool(own_active or owned and queue_busy),
            "results": len(current.results) if current.results else (BatchManager.get_completed_count() if owned else 0),
            "busy": busy, "can_start": not busy, "can_stop": can_stop,
            "state": state, "message": message}


def _own_results():
    with _task_start_lock:
        results = list(_get_sess(session["sid"]).results)
        if not results and _owns_task(BatchManager.get_active_task()):
            results = BatchManager.get_all_results()
        return results


def _control_guard(admin_only=False):
    """全局控制与启动使用同一把锁；维护操作只有管理员可以调用。"""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with _task_start_lock:
                if admin_only and not is_user_admin(session["user_id"]):
                    return jsonify({"ok": False, "error": "仅管理员可以执行服务维护"}), 403
                if _maintenance_pending or _startup_recovery_pending:
                    return jsonify({"ok": False, "error": "服务正在重启"}), 409
                if admin_only:
                    if _any_task_busy():
                        return jsonify({"ok": False, "error": "仍有用户任务运行或清理中，不能执行全局维护"}), 403
                    with system_task_scope():
                        return function(*args, **kwargs)
                return function(*args, **kwargs)
        return wrapped
    return decorate


# ── 认证路由 ─────────────────────────────────────────────────
@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip()
    password = data.get("password", "")

    ok, user_id = verify_user(username, password)
    if not ok:
        return jsonify({"ok": False, "msg": "用户名或密码错误"})

    session.clear()
    session["user_id"] = user_id
    session["username"] = username
    # 用 user_id 作为 sid，这样同一用户在不同设备共享任务状态
    session["sid"] = f"user_{user_id}"
    return jsonify({"ok": True, "msg": "登录成功"})


@app.route("/auth/register", methods=["POST"])
@admin_required
def auth_register():
    """只有管理员能注册新用户"""
    data = request.get_json(force=True)
    username = data.get("username", "").strip()
    password = data.get("password", "")

    ok, msg = create_user(username, password)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    if "user_id" in session:
        return jsonify({"ok": True, "username": session.get("username", ""),
                        "user_id": session["user_id"],
                        "is_admin": is_user_admin(session["user_id"])})
    return jsonify({"ok": False})


# ── 管理员 API ────────────────────────────────────────────────
@app.route("/admin")
@app.route("/admin/")
@admin_required
def admin_page():
    return render_template("admin.html", username=session.get("username", ""),
                           current_user_id=session["user_id"])


@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    response = jsonify({"ok": True, **get_admin_stats()})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_page(user_id):
    user = get_admin_user_identity(user_id)
    if user is None:
        abort(404)
    return render_template("admin_user.html", target_user=user,
                           current_user_id=session["user_id"], today=datetime.now().date().isoformat())


@app.route("/api/admin/users/<int:user_id>/details")
@admin_required
def admin_user_details(user_id):
    try:
        details = get_admin_user_details(user_id, request.args.get("start") or None,
                                         request.args.get("end") or None, request.args.get("group", "day"))
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if details is None:
        return jsonify({"ok": False, "error": "用户不存在或没有保留的统计"}), 404
    return jsonify({"ok": True, **details})


@app.route("/admin/users")
@admin_required
def admin_list_users():
    return jsonify({"ok": True, "users": list_users()})


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    data = request.get_json(force=True)
    username = data.get("username", "").strip()
    password = data.get("password", "")
    is_admin = data.get("is_admin", False)
    ok, msg = create_user(username, password, is_admin=is_admin)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/admin/users/delete", methods=["POST"])
@admin_required
def admin_delete_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if user_id == session["user_id"]:
        return jsonify({"ok": False, "error": "不能删除自己"})
    ok = delete_user(user_id)
    return jsonify({"ok": ok, "error": "" if ok else "用户不存在或不能删除管理员"})


@app.route("/auth/password", methods=["POST"])
@login_required
def auth_change_password():
    data = request.get_json(force=True)
    ok = change_password(session["user_id"], data.get("password", ""))
    return jsonify({"ok": ok, "msg": "密码已修改" if ok else "密码至少4位"})


@app.route("/admin/users/password", methods=["POST"])
@admin_required
def admin_change_password():
    data = request.get_json(force=True)
    is_self = data.get("self", False)
    new_password = data.get("password", "")

    if is_self:
        # 用户自己改密码
        ok = change_password(session["user_id"], new_password)
        return jsonify({"ok": ok, "msg": "密码已修改" if ok else "密码至少4位"})

    user_id = data.get("user_id")
    ok = change_password(user_id, new_password)
    return jsonify({"ok": ok, "msg": "密码已修改" if ok else "密码至少4位"})


@app.route("/admin/users/policy", methods=["POST"])
@admin_required
def admin_user_policy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("user_id")) is not int:
        return jsonify({"ok": False, "error": "用户 ID 必须为整数"}), 400
    with _task_start_lock:
        try:
            ok = save_user_policy(data["user_id"], data.get("require_proxy"), data.get("max_concurrency"))
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "用户不存在"}), 404
    return jsonify({"ok": True, "msg": "已保存，下次任务及后续批次生效"})


# ── 历史记录 API（服务端持久化）────────────────────────────────
@app.route("/api/history")
@login_required
def api_history():
    """获取当前用户的历史记录"""
    user_id = session["user_id"]
    all_rows = request.args.get("all", "0")
    filter_type = request.args.get("filter", "all")
    if all_rows not in ("0", "1") or filter_type not in ("all", "success", "failed"):
        return jsonify({"ok": False, "error": "无效的历史筛选条件"}), 400
    success = None if filter_type == "all" else filter_type == "success"
    rows = get_history(user_id, limit=None if all_rows == "1" else 500, success=success)
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/history/delete", methods=["POST"])
@login_required
def api_history_delete():
    """删除指定历史记录"""
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    user_id = session["user_id"]
    count = delete_history_items(user_id, ids)
    return jsonify({"ok": True, "deleted": count})


@app.route("/api/history/clear", methods=["POST"])
@login_required
def api_history_clear():
    """清空历史"""
    user_id = session["user_id"]
    count = db_clear_history(user_id)
    return jsonify({"ok": True, "deleted": count})


# ── 用户专属设置 API（服务端多端同步）──────────────────────────
@app.route("/api/settings")
@login_required
def api_get_settings():
    """获取当前用户的专属设置"""
    user_id = session["user_id"]
    settings = get_user_settings(user_id)
    return jsonify({"ok": True, "settings": settings, "policy": get_user_policy(user_id)})


@app.route("/api/policy")
@login_required
def api_user_policy():
    return jsonify({"ok": True, "policy": get_user_policy(session["user_id"])})


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    """保存当前用户的专属设置"""
    user_id = session["user_id"]
    data = request.get_json(force=True) or {}
    if "concurrency" in data:
        policy = get_user_policy(user_id)
        try:
            data["concurrency"] = _requested_concurrency(data["concurrency"], policy["max_concurrency"])
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
    for key in ["accounts", "proxy", "headless", "products", "concurrency"]:
        if key in data:
            save_user_setting(user_id, key, str(data[key]))
    return jsonify({"ok": True})


# ── 主页 ─────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html", sid=session.get("sid", ""),
                           username=session.get("username", ""),
                           is_admin=is_user_admin(session["user_id"]),
                           user_id=session["user_id"], policy=get_user_policy(session["user_id"]),
                           products=PRODUCT_COLUMNS, product_short=PRODUCT_SHORT)


@app.route("/api/status")
@login_required
def api_status():
    with _task_start_lock:
        return jsonify(_task_status())


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("accounts", ""), str):
        return jsonify({"ok": False, "error": "请提交有效的账号文本"}), 400
    sid  = session["sid"]
    s    = _get_sess(sid)

    with _task_start_lock:
        if _task_busy():
            status = _task_status()
            return jsonify({"ok": False, "error": status["message"], "status": status}), 400

        raw_accounts = data.get("accounts", "").strip()
        headless     = data.get("headless", False)
        if sys.platform != "win32" and "DISPLAY" not in os.environ:
            headless = True
        proxy        = data.get("proxy")
        vless_proxy  = data.get("vless_proxy", "").strip() or None
        selected_products = data.get("products", None)  # 用户选择的产品列表
        policy = get_user_policy(session["user_id"])
        if policy is None:
            return jsonify({"ok": False, "error": "用户不存在", "need_login": True}), 401
        try:
            concurrency = _requested_concurrency(data.get("concurrency", min(3, policy["max_concurrency"])),
                                                 policy["max_concurrency"])
            proxy_pool = _validated_proxy_pool(proxy)
            if policy["require_proxy"] and not proxy_pool:
                return jsonify({"ok": False, "error": "管理员要求此账号必须填写代理池，不能直连", "policy": policy}), 403
            proxy = "\n".join(proxy_pool) or None
            batch_size = max(0, int(data.get("batch_size", getattr(config, "BATCH_SIZE", 0))))
        except (TypeError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error), "policy": policy}), 400
        mode         = data.get("mode", "azure_student").strip()

        if not raw_accounts:
            return jsonify({"ok": False, "error": "账号列表为空"}), 400

        accounts = _parse_accounts(raw_accounts)
        if not accounts:
            return jsonify({"ok": False, "error": "账号格式错误，每行: 邮箱 TAB 密码 (可选 TAB TOTP密钥)"}), 400

        s.running = True
        s.worker_active = True
        s.results = []
        s.log_history.clear()
        for q in list(s.listeners):
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break

        try:
            # 初始化批次任务管理与持久化队列
            BatchManager.init_task(
                sid=sid,
                user_id=session.get("user_id"),
                accounts=accounts,
                headless=headless,
                proxy=proxy,
                vless_proxy=vless_proxy,
                selected_products=selected_products,
                concurrency=concurrency,
                mode=mode,
                batch_size=batch_size,
            )

            threading.Thread(
                target=_run_batch_worker, args=(sid,), daemon=True
            ).start()
        except Exception:
            s.running = False
            s.worker_active = False
            BatchManager.clear_task()
            app.logger.exception("启动任务失败")
            return jsonify({"ok": False, "error": "启动任务失败，请检查服务日志"}), 500
        return jsonify({"ok": True, "count": len(accounts), "concurrency": concurrency, "policy": policy})




@app.route("/api/check_proxy", methods=["POST"])
@login_required
def api_check_proxy():
    data       = request.get_json(force=True)
    proxy_str  = data.get("proxy")
    vless_str  = data.get("vless_proxy", "").strip()
    policy = get_user_policy(session["user_id"])
    try:
        proxy_list = _validated_proxy_pool(proxy_str)
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if policy["require_proxy"] and not proxy_list:
        return jsonify({"ok": False, "error": "管理员要求此账号必须填写代理池，不能直连"}), 403

    ip_check_urls = [
        ("https://api.ipify.org?format=json",  "json", "ip"),
        ("https://api4.my-ip.io/ip.json",       "json", "ip"),
        ("http://ip-api.com/json",              "json", "query"),
        ("https://checkip.amazonaws.com/",      "text", None),
    ]

    def _get_ip(proxies=None):
        last_err = "所有 IP 检测服务均无响应"
        for url, fmt, key in ip_check_urls:
            try:
                r = _requests.get(url, proxies=proxies, timeout=15, verify=False)
                r.raise_for_status()
                if fmt == "text":
                    ip = r.text.strip()
                else:
                    j = r.json()
                    ip = j.get(key) if key else list(j.values())[0]
                if ip:
                    return str(ip), None
            except Exception as e:
                last_err = str(e)
        return None, last_err

    if not proxy_list:
        ip, err = _get_ip()
        if ip:
            return jsonify({"ok": True, "ip": ip, "via_proxy": False, "msg": f"本机直连 IP: {ip}"})
        return jsonify({"ok": False, "error": f"无法获取本机 IP: {err}"})

    # 全量检测代理池（并发检测所有节点，解除前10个上限）
    valid_count = 0
    results_map = {}

    def _check_one(idx_and_purl):
        idx, p_url = idx_and_purl
        if vless_str:
            chain = XrayProxyChain(vless_str, p_url)
            try:
                local_url = chain.start()
                proxies   = {"http": local_url, "https": local_url}
                ip, err   = _get_ip(proxies)
            except Exception as e:
                ip, err = None, str(e)
            finally:
                chain.stop()
        else:
            proxies = {"http": p_url, "https": p_url}
            ip, err = _get_ip(proxies)
        return idx, p_url, ip, err

    max_workers = min(len(proxy_list), policy["max_concurrency"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(copy_context().run, _check_one, (i, p)) for i, p in enumerate(proxy_list)]
        for f in concurrent.futures.as_completed(futures):
            idx, p_url, ip, err = f.result()
            results_map[idx] = (ip, err)

    details = []
    for idx in range(len(proxy_list)):
        ip, err = results_map[idx]
        if ip:
            valid_count += 1
            details.append(f"节点 {idx+1}: 可用 (IP: {ip})")
        else:
            details.append(f"节点 {idx+1}: 失败 ({err})")

    msg = f"代理池检测完成: {valid_count}/{len(proxy_list)} 个代理可用\n" + "\n".join(details)
    return jsonify({"ok": valid_count > 0, "msg": msg, "ip": details[0] if details else "", "via_proxy": True})



@app.route("/api/stop", methods=["POST"])
@login_required
@_control_guard()
def api_stop():
    data = request.get_json(force=True) or {}
    sid  = session["sid"]
    s    = _get_sess(sid)
    if not _owns_task(BatchManager.get_active_task()):
        return jsonify({"ok": True, "killed": 0})
    if not s.running and not s.worker_active and not BatchManager.is_running() and BatchManager.get_active_task().get("status") != "all_done_restarting":
        return jsonify({"ok": True, "killed": 0})
    s.running = False
    BatchManager.stop_task()
    if s.task_loop and s.task_ref and not s.task_ref.done():
        s.task_loop.call_soon_threadsafe(s.task_ref.cancel)
        _push_log(sid, "⚠️  停止指令已发送，正在中止...")
    else:
        _push_log(sid, "⚠️  无运行中的任务")
    
    # 立即同步杀死所有后台浏览器与驱动进程
    killed = BrowserProcessManager.cleanup_all()
    release_system_memory()
    if killed > 0:
        _push_log(sid, f"🧹 已立即强制终止 {killed} 个残留浏览器/驱动/代理进程并回收内存")
    return jsonify({"ok": True, "killed": killed})


@app.route("/api/restart", methods=["POST"])
@login_required
@_control_guard(admin_only=True)
def api_restart():
    """手动触发服务强制重启并彻底清理所有残留进程与系统内存。"""
    data = request.get_json(force=True) or {}
    sid  = session["sid"]
    _push_log(sid, "🔄 正在手动触发服务重启以彻底清空内存与杀死全部残留进程...")
    global _maintenance_pending
    _maintenance_pending = True
    def restart():
        global _maintenance_pending
        try:
            BatchManager.trigger_service_restart(1.0)
        finally:
            with _task_start_lock:
                _maintenance_pending = False
    try:
        threading.Thread(target=restart, daemon=True).start()
    except Exception:
        _maintenance_pending = False
        raise
    return jsonify({"ok": True, "msg": "服务正在重启释放全部内存"})


@app.route("/api/cleanup", methods=["POST"])
@login_required
@_control_guard(admin_only=True)
def api_cleanup():
    """立即深度清理所有残留浏览器与驱动进程并回收内存。"""
    sid  = session["sid"]
    killed = BrowserProcessManager.cleanup_all(global_scope=True)
    release_system_memory()
    if sid:
        _push_log(sid, f"🧹 已彻底清理所有残留进程 (清理 {killed} 个进程) 并归还系统内存")
    return jsonify({"ok": True, "killed": killed})


@app.route("/api/results")
@login_required
def api_results():
    sid  = session["sid"]
    results = _own_results()
    rows = []
    for r in results:
        rows.append({
            "email":       r.account.email,
            "password":    r.account.password,
            "success":     r.success,
            "totp_secret": r.totp_secret,
            "keys":        r.keys,
            "msg":         r.message,
            "ts":          r.ts,
        })
    return jsonify(rows)


@app.route("/api/export", methods=["POST"])
@login_required
def api_export():
    """导出当前 session 结果为 Excel，返回文件下载。"""
    data   = request.get_json(force=True) or {}
    sid    = session["sid"]
    filter_type = data.get("filter", "all")  # all / success / failed

    results = _own_results()
    if filter_type == "success":
        results = [r for r in results if r.success]
    elif filter_type == "failed":
        results = [r for r in results if not r.success]

    if not results:
        return jsonify({"ok": False, "error": "没有可导出的数据"}), 400

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if filter_type not in ("all", "success", "failed"):
        return jsonify({"ok": False, "error": "未知导出筛选条件"}), 400
    fname = f"azure_keys_{session['user_id']}_{filter_type}_{ts}_{uuid.uuid4().hex}.xlsx"
    fpath = export_to_excel(results, fname)

    return send_file(fpath, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/stream")
@login_required
def stream():
    sid = session["sid"]
    user_id = session["user_id"]
    s   = _get_sess(sid)

    # 为每台连入的终端标签页/设备创建独立的日志队列并注册，实现广播分发
    q = queue.Queue()
    s.listeners.append(q)

    def gen():
        try:
            # 1. 补发历史日志，使多端连入时能秒同步已生成的日志历史
            for item in list(s.log_history):
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

            # 2. 持续订阅新的日志广播
            while True:
                if not user_exists(user_id):
                    return
                try:
                    item = q.get(timeout=25)
                    if not user_exists(user_id):
                        return
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield 'data: {"ping":1}\n\n'
        finally:
            # 3. 连接断开时，自动移除当前终端的监听队列
            try:
                s.listeners.remove(q)
            except Exception:
                pass

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 账号解析 ─────────────────────────────────────────────────
# 中文/英文 "账号" / "密码" 标签
_LABEL_ACC = r"(?:账[号戶]|帳[号戶]|邮箱|郵箱|用户名|用戶名|账户|帳戶|account|email|user(?:name)?|登录?名?|登錄?名?)"
_LABEL_PWD = r"(?:密码|密碼|口令|password|pwd|pass)"
# 一对中英文冒号或等号，前后可空白（含全角冒号 U+FF1A）
_SEP = r"\s*[:\uff1a=]\s*"

# 形如:  账号: aaa@bbb.com 密码: xxxx
# 多对账号可以挤在同一行（无分隔），密码非贪婪匹配，遇到下一个 "账号" 标签或行末/串末停止
_RE_LABELED = re.compile(
    rf"{_LABEL_ACC}{_SEP}(\S+?@\S+?)\s*{_LABEL_PWD}{_SEP}(.+?)(?={_LABEL_ACC}{_SEP}|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
# 仅一侧带标签时的兜底（用于跨行场景）
_RE_ACC_ONLY = re.compile(rf"^\s*{_LABEL_ACC}{_SEP}(\S+@\S+)\s*$", re.IGNORECASE)
_RE_PWD_ONLY = re.compile(rf"^\s*{_LABEL_PWD}{_SEP}(\S+)\s*$", re.IGNORECASE)


def _parse_accounts(raw: str) -> list[Account]:
    """
    每行一个账号，支持以下格式:
      1. edu邮箱<TAB>密码
      2. edu邮箱----密码
      3. edu邮箱 空格 密码
      4. 账号: edu邮箱 密码: xxxx          (中/英文标签，单行)
      5. 账号: edu邮箱\n密码: xxxx          (中/英文标签，跨行)
    注释行以 # 开头会被忽略。
    """
    accounts: list[Account] = []
    if not raw:
        return accounts

    # 先尝试逐行解析常规分隔符（兼容旧格式）
    consumed_spans: list[tuple[int, int]] = []
    for m in _RE_LABELED.finditer(raw):
        email = m.group(1).strip().rstrip(",;")
        pwd = m.group(2).strip().rstrip(",;")
        if email and pwd:
            accounts.append(Account(email=email, password=pwd))
            consumed_spans.append(m.span())

    # 把已经匹配到的"标签格式"内容掩码掉，剩余部分按行分隔符解析
    if consumed_spans:
        masked = list(raw)
        for s, e in consumed_spans:
            for i in range(s, e):
                masked[i] = " "
        leftover = "".join(masked)
    else:
        leftover = raw

    for line in leftover.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 跨行的 "账号:xxx" / "密码:xxx" 兜底（两行都剩）— 简单匹配单侧
        if _RE_ACC_ONLY.search(line) or _RE_PWD_ONLY.search(line):
            # 跨行情况：先收集本块所有标签
            continue
        if "----" in line:
            parts = [p.strip() for p in line.split("----") if p.strip()]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
        else:
            parts = [p.strip() for p in line.split() if p.strip()]
        if len(parts) < 2:
            continue
        totp = parts[2] if len(parts) >= 3 else ""
        accounts.append(Account(email=parts[0], password=parts[1], totp_secret=totp))

    # 跨行 "账号:xxx\n密码:xxx" 兜底解析
    pending_email: str | None = None
    for line in leftover.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ma = _RE_ACC_ONLY.search(line)
        mp = _RE_PWD_ONLY.search(line)
        if ma and not mp:
            pending_email = ma.group(1).strip().rstrip(",;")
        elif mp and pending_email:
            pwd = mp.group(1).strip().rstrip(",;")
            if pwd:
                accounts.append(Account(email=pending_email, password=pwd))
            pending_email = None

    # 去重（保留先出现的）
    seen = set()
    uniq: list[Account] = []
    for a in accounts:
        key = (a.email.lower(), a.password)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)
    return uniq


# ── 后台任务 (多批次运行与自动重启控制) ─────────────────────────
def _run_batch_worker(sid: str):
    match = re.fullmatch(r"user_([1-9]\d*)", sid)
    if not match:
        return
    user_id = int(match.group(1))
    s = _get_sess(sid)
    with task_scope(user_id):
        s.worker_active = True
        try:
            task = BatchManager.get_active_task()
            if not task or task.get("sid") != sid or task.get("user_id") != user_id:
                s.running = False
                return
            if not user_exists(user_id):
                _push_log(sid, "⚠️ 用户已删除，任务不再恢复")
                BatchManager.stop_task()
                return
            with task_scope(user_id, task.get("selected_products") or PRODUCT_COLUMNS), totp_cache_scope(user_id):
                try:
                    for row in get_history(user_id):
                        email, secret = row.get("email"), row.get("totp_secret")
                        if email and secret and not get_cached_totp(email):
                            save_totp_cache(email, secret)
                except Exception:
                    app.logger.warning("用户 2FA 历史缓存补充失败，将使用用户输入及专属缓存")
                return _run_batch_worker_impl(sid)
        except Exception:
            app.logger.exception("后台任务异常退出")
            _push_log(sid, "❌ 后台任务异常退出，已释放占用，请检查服务日志后重新启动")
            with _task_start_lock:
                BatchManager.mark_failed(sid)
        finally:
            try:
                BrowserProcessManager.cleanup_all()
            finally:
                with _task_start_lock:
                    s.running = False
                    s.worker_active = False
                close_db()


def _run_batch_worker_impl(sid: str):
    if current_task_user() is None or sid != f"user_{current_task_user()}":
        raise ValueError("用户任务必须在自己的范围内运行")
    s  = _get_sess(sid)
    pl = lambda msg: _push_log(sid, msg)

    task = BatchManager.get_active_task()
    if not task or task.get("sid") != sid or task.get("status") not in ("running", "batch_restarting"):
        s.running = False
        return

    batch_accounts, batch_idx, total_batches, offset, total_accs = BatchManager.get_current_batch()
    s.running = True
    headless = task.get("headless", False)
    proxy = task.get("proxy")
    vless_proxy = task.get("vless_proxy")
    selected_products = task.get("selected_products")
    user_id = task.get("user_id")
    concurrency = int(task.get("concurrency", 3))
    mode = task.get("mode", "azure_student")

    _proxy_label = (
        f"VLESS链式" if vless_proxy else ('代理池模式' if proxy else '直连')
    )
    _mode_label = "微软账号 Key 直提 (免认证)" if mode == "direct_ms" else "Azure 学生认证 + Key 提取"
    
    try:
        while s.running:
            task = BatchManager.get_active_task()
            if not task or task.get("status") not in ("running", "batch_restarting"):
                break

            # 重启恢复与后续批次同样校验最新策略，不能绕过后台限制。
            policy = get_user_policy(user_id)
            if policy is None:
                pl("⚠️ 用户已删除，任务已停止")
                BatchManager.stop_task()
                break
            concurrency = max(1, min(int(task.get("concurrency", 3)), policy["max_concurrency"]))
            try:
                pool = _validated_proxy_pool(proxy)
                if policy["require_proxy"] and not pool:
                    raise ValueError("管理员已要求使用代理，请填写代理池后重新启动任务")
            except ValueError as error:
                pl(f"⚠️ {error}")
                BatchManager.stop_task()
                break

            batch_accounts, batch_idx, total_batches, offset, total_accs = BatchManager.get_current_batch()
            pl(f"{'═'*50}")
            if total_batches > 1:
                pl(f"📦 启动批次 [{batch_idx + 1}/{total_batches}]  本批账号数={len(batch_accounts)}  总账号数={total_accs} (进度: {offset + 1}~{offset + len(batch_accounts)}/{total_accs})")
            else:
                pl(f"📦 启动任务  总账号数={total_accs}")
            pl(f"⚡ 运行模式={_mode_label}  并发进程={concurrency}  代理={_proxy_label}")
            pl(f"📦 目标产品: {len(config.get_products_to_extract())} 个")
            pl(f"{'═'*50}")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                # 每完成一个账号的回调：立即追加到 session results + 持久化到数据库与批次记录
                def _on_account_done(result):
                    if result is not None:
                        s.results.append(result)
                        BatchManager.record_account_result(result)
                        # 持久化到数据库
                        if user_id:
                            try:
                                save_result(user_id, {
                                    "email": result.account.email,
                                    "password": result.account.password,
                                    "totp_secret": result.totp_secret or "",
                                    "success": result.success,
                                    "keys": result.keys,
                                    "msg": result.message,
                                    "ts": result.ts,
                                })
                            except Exception as e:
                                pl(f"⚠️  保存记录失败: {e}")

                async def _inner():
                    return await run_pipeline(
                        [a for a in batch_accounts if s.running],
                        headless=headless,
                        proxy_str=proxy,
                        vless_str=vless_proxy,
                        cb=pl,
                        on_result=_on_account_done,
                        concurrency=concurrency,
                        mode=mode,
                        batch_index=batch_idx,
                        total_batches=total_batches,
                        overall_offset=offset,
                        overall_total=total_accs,
                    )

                task_coro = loop.create_task(_inner())
                s.task_loop = loop
                s.task_ref  = task_coro

                try:
                    batch_results = loop.run_until_complete(task_coro)
                    
                    b_ok   = sum(1 for r in batch_results if r and r.success)
                    b_fail = len([r for r in batch_results if r]) - b_ok
                    if total_batches > 1:
                        pl(f"{'─'*50}")
                        pl(f"📊 第 {batch_idx + 1}/{total_batches} 批次完成  ✅ 成功: {b_ok}  ❌ 失败: {b_fail}  本批共: {len(batch_results)}")
                        pl(f"{'─'*50}")

                    # 检查是否还有后续批次
                    has_next = BatchManager.has_next_batch()

                    if has_next:
                        # 网页任务仅清理自己的进程后继续，绝不重启整个服务。
                        BatchManager.advance_to_next_batch(restarting=False)
                        pl("🧹 当前批次已完成，清理当前用户的浏览器后继续下一批，不影响其他用户")
                        BrowserProcessManager.cleanup_all()
                        continue
                    else:
                        # 所有批次全部完成！
                        BatchManager.mark_all_done(restarting=False)
                        all_results = BatchManager.get_all_results() or s.results
                        ok   = sum(1 for r in all_results if r.success)
                        fail = len(all_results) - ok

                        pl(f"{'═'*50}")
                        pl(f"🎉 全部 {total_accs} 个账号已全部处理完毕！")
                        pl(f"📊 最终总览  ✅ 成功: {ok}  ❌ 失败: {fail}  总计: {len(all_results)}")
                        pl(f"{'═'*50}")

                        for prod in PRODUCT_COLUMNS:
                            count = sum(1 for r in all_results if r.keys.get(prod))
                            short = PRODUCT_SHORT.get(prod, prod[:20])
                            pl(f"  📦 {short}: {count}/{len(all_results)} 个 key")

                        pl(f"{'═'*50}")

                        # 自动导出汇总 Excel
                        if all_results:
                            try:
                                fpath = export_to_excel(all_results, f"azure_keys_{user_id}_{uuid.uuid4().hex}.xlsx")
                                pl(f"📥 最终完整 Excel 已保存: {os.path.basename(fpath)}")
                            except Exception as e:
                                pl(f"⚠️  Excel 导出失败: {e}")

                        pl("🏁 任务结束")

                        break

                except asyncio.CancelledError:
                    pl("⚠️  任务已被中止")
                    BatchManager.stop_task()
                    break
                except Exception as e:
                    pl(f"❌ 任务运行异常: {e}")
                    BatchManager.mark_failed(sid)
                    break
                finally:
                    s.task_loop = None
                    s.task_ref  = None
            finally:
                loop.close()
    finally:
        s.running = False
        try:
            killed = BrowserProcessManager.cleanup_all()
            pl(f"🧹 当前用户任务已结束，已清理自己的浏览器、驱动及代理进程 (共清理 {killed} 个进程)")
        except Exception:
            pass
        release_system_memory()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5010, debug=False, threaded=True)
