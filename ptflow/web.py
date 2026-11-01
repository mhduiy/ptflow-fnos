"""Authenticated local/native web surface; no credentials in public responses."""
from collections import defaultdict, deque
from datetime import timedelta
from threading import RLock
from urllib.parse import urlsplit
import hmac
import io
import secrets
import time

from flask import Flask, jsonify, request, send_file, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__
from .config import Config, validate_config
from .engine import Engine
from .scheduler import Scheduler
from .store import Store


class TransportSession(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return request.is_secure


class GatewayPrefix:
    def __init__(self, application, prefix):
        self.application, self.prefix = application, prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and (path == self.prefix or path.startswith(self.prefix + "/")):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.application(environ, start_response)


def create_app(data_dir, gateway_prefix="", start_scheduler=True):
    config = Config(data_dir)
    store = Store(data_dir)
    store.secrets = config.secrets()
    scheduler = Scheduler(config, store)
    app = Flask(__name__, static_folder="static")
    app.config.update(SECRET_KEY=config.auth["secret_key"], MAX_CONTENT_LENGTH=65536,
                      SESSION_COOKIE_NAME="ptflow_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
                      SESSION_COOKIE_PATH=gateway_prefix.rstrip("/") + "/" if gateway_prefix else "/")
    app.session_interface = TransportSession()
    if gateway_prefix:
        app.wsgi_app = GatewayPrefix(ProxyFix(app.wsgi_app, x_proto=1, x_host=1), gateway_prefix)
    app.extensions.update(ptflow_config=config, ptflow_store=store, ptflow_scheduler=scheduler)
    attempts, attempt_lock = defaultdict(deque), RLock()

    def authenticated():
        return bool(config.auth["password_hash"] and session.get("authenticated") and session.get("generation") == config.auth["generation"])

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    @app.before_request
    def authorize():
        if not request.path.startswith("/api/"):
            return None
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin", "")
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != request.host.lower():
                return jsonify(error="请求来源不匹配，请从 PTFlow 页面操作"), 403
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, session.get("csrf_token", "")):
                return jsonify(error="安全令牌已过期，请刷新页面后重试"), 403
            if not request.is_json:
                return jsonify(error="请使用 JSON 请求"), 415
        if request.path not in ("/api/auth", "/api/login", "/api/setup") and not authenticated():
            return jsonify(error="请先登录 PTFlow"), 401
        return None

    @app.after_request
    def protect_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, ValueError):
            return jsonify(error=store.redact(str(error))), 400
        store.event("ERROR", "Web", f"请求失败：{request.path}；{type(error).__name__}：{error}")
        return jsonify(error=store.redact(f"操作失败：{type(error).__name__}：{error}")), 500

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def establish_session():
        session.clear()
        session.update(authenticated=True, generation=config.auth["generation"], csrf_token=secrets.token_urlsafe(32))
        session.permanent = True
        return jsonify(authenticated=True, csrf_token=session["csrf_token"])

    def validate_password(password):
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ValueError("管理密码需要 10–256 个字符")
        return password

    def rate_limit():
        identity, now = request.remote_addr, time.monotonic()
        with attempt_lock:
            queue = attempts[identity]
            while queue and queue[0] < now - 600:
                queue.popleft()
            if len(queue) >= 10:
                return True
            queue.append(now)
            if len(attempts) > 1000:
                for key in list(attempts):
                    if attempts[key] and attempts[key][-1] < now - 600:
                        del attempts[key]
        return False

    @app.get("/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/auth")
    def auth_status():
        return jsonify(setup_required=not bool(config.auth["password_hash"]), authenticated=authenticated(), csrf_token=csrf_token())

    @app.post("/api/setup")
    def setup():
        if gateway_prefix and request.headers.get("X-Trim-Isadmin", "").lower() not in ("true", "1"):
            return jsonify(error="首次初始化仅允许飞牛管理员，请从管理员桌面打开 PTFlow"), 403
        password = validate_password(body().get("password"))
        with config.lock:
            if config.auth["password_hash"]:
                return jsonify(error="初始化已完成，请登录"), 409
            config.set_password_hash(generate_password_hash(password))
        store.event("INFO", "安全", "已设置管理密码；自动刷流仍需在配置页主动开启")
        return establish_session()

    @app.post("/api/login")
    def login():
        if rate_limit():
            return jsonify(error="登录尝试过多，请 10 分钟后重试"), 429
        password = body().get("password", "")
        if not isinstance(password, str) or len(password) > 256 or not config.auth["password_hash"] or not check_password_hash(config.auth["password_hash"], password):
            return jsonify(error="管理密码不正确"), 401
        return establish_session()

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/password")
    def password():
        value = body()
        current = value.get("current_password", "")
        if not isinstance(current, str) or len(current) > 256 or not check_password_hash(config.auth["password_hash"], current):
            return jsonify(error="当前密码不正确"), 403
        config.set_password_hash(generate_password_hash(validate_password(value.get("password"))))
        store.event("INFO", "安全", "管理密码已更新，其他会话已失效")
        return establish_session()

    @app.get("/api/config")
    def get_config():
        return jsonify(config.public())

    @app.put("/api/config")
    def put_config():
        old_secrets = config.secrets()
        result = config.save(body())
        store.secrets = list(set(store.secrets + old_secrets + config.secrets()))
        scheduler.reschedule()
        store.event("INFO", "配置", "配置已保存并重排下一次执行时间；正在运行的任务继续使用启动时配置", details={"query_minutes": result["schedule"]["query_minutes"], "brush_minutes": result["schedule"]["brush_minutes"], "automatic": result["schedule"]["enabled"]})
        return jsonify(result)

    @app.post("/api/test")
    def test_connection():
        value = body()
        target = value.get("target")
        if target not in ("downloader", "mteam"):
            raise ValueError("连接测试目标应为 downloader 或 mteam")
        draft = validate_config(value.get("config", {}), config.get())
        # Draft secrets must be redacted even when the user has not saved them.
        draft_secrets = [draft["downloader"]["password"], draft["downloader"]["api_key"], draft["mteam"]["api_key"]]
        store.secrets = list(set(store.secrets + draft_secrets))
        engine = Engine(draft, store, lambda stage, message: None)
        try:
            return jsonify(engine.test_connection(target))
        except Exception as error:
            message = store.redact(f"{type(error).__name__}：{error}")
            store.event("ERROR", "连接测试", f"{target} 连接失败：{message}")
            return jsonify(ok=False, message=message)

    @app.get("/api/status")
    def status():
        return jsonify(scheduler.status())

    @app.post("/api/run")
    def run():
        kind = body().get("kind", "full")
        try:
            identifier = scheduler.submit(kind)
        except RuntimeError as error:
            return jsonify(error=str(error)), 409
        return jsonify(id=identifier, message="任务已启动，请查看当前阶段与实时日志"), 202

    @app.get("/api/torrents")
    def torrents():
        try:
            items = Engine(config.get(), store, lambda stage, message: None).torrents()
            return jsonify(items=items, error=None)
        except Exception as error:
            return jsonify(items=[], error=store.redact(f"无法读取 QB 种子：{type(error).__name__}：{error}"))

    @app.get("/api/candidates")
    def candidates():
        return jsonify(items=store.candidates())

    def integer_argument(key, default, minimum, maximum):
        try:
            value = int(request.args.get(key, default))
        except (ValueError, TypeError):
            raise ValueError(f"{key} 必须是整数") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum}–{maximum} 之间")
        return value

    @app.get("/api/history")
    def history():
        return jsonify(items=store.history(integer_argument("minutes", 60, 1, 10080)))

    @app.get("/api/runs")
    def runs():
        return jsonify(items=store.runs())

    @app.get("/api/logs")
    def logs():
        after = integer_argument("after", 0, 0, 2**63 - 1)
        limit = integer_argument("limit", 200, 1, 500)
        oldest, newest = store.event_bounds()
        reset = bool(after and oldest and after < oldest - 1)
        items = store.events(after, limit, request.args.get("level", ""), request.args.get("stage", ""), request.args.get("q", "")[:256], request.args.get("run_id", ""))
        cursor = items[-1]["id"] if items else max(after, newest)
        return jsonify(items=items, cursor=cursor, oldest_id=oldest, reset=reset)

    @app.get("/api/logs/export")
    def export_logs():
        rows = store.events(limit=10000)
        text = "\n".join(f"{row['time']} [{row['level']}] [{row['stage']}] run={row['run_id'] or '-'} {row['message']} {row['details']}" for row in rows) + "\n"
        return send_file(io.BytesIO(text.encode()), mimetype="text/plain; charset=utf-8", as_attachment=True, download_name="ptflow-logs.txt")

    if start_scheduler:
        scheduler.start()
    return app
