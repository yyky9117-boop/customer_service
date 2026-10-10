"""本机客服网页与 API。只暴露注册路由，不提供项目文件目录。"""
import argparse
import asyncio
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from concurrent.futures import Future
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from .catalog import Catalog, ROOT, instant
from .errors import ServiceFailure
from .model import DeepSeekModel, load_env
from .recommendation import RecommendationModule
from .reply import ReplyGenerator
from .service import ChatService, SessionStore
from .validation import ContractViolation, validate_request
from integration.service import UnifiedChatService
from integration.store import UnifiedSessionStore
from integration.identity import IdentityBridge, DEMO_BINDINGS
from integration.order_adapter import LegacyOrderRepository, OrderQueryAdapter
from integration.legacy_loader import load as load_order, SharedOrderReplyModel, make_reply_generator
from integration.router import ConversationRouter, RoutingClarification

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
PROFILE_LABELS = {"U_GUEST": "匿名顾客", "U_NEW": "普通新客 · 订单账号 A", "U_RETURN": "普通老客 · 订单账号 B", "U_VIP": "VIP 会员",
    "U_UNKNOWN": "资料待确认", "U_LOCKED": "偏远地区顾客"}


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        super().server_close()
        if hasattr(self, "app"):
            self.app.close()


class WebApp:
    def __init__(self, database=None, model=None, catalog=None, *, order_root=None, order_database=None, courier=None, start_mock_courier=False):
        self.catalog = catalog or Catalog.load()
        self.model = model if model is not None else DeepSeekModel()
        self.store = UnifiedSessionStore(database or ROOT / ".local" / "web-sessions.sqlite3")
        self.reply_generator = ReplyGenerator(self.model, self.catalog) if isinstance(self.model, DeepSeekModel) else None
        self.service = UnifiedChatService(RecommendationModule(self.catalog, self.model), self.store,
            clock=lambda: instant(self.catalog.data["manifest"]["reference_time"]), reply_generator=self.reply_generator,
            router=ConversationRouter())
        self.courier_server = self.courier_thread = None
        legacy = order_root or ROOT.parent / "snack_customer_service"
        identities = IdentityBridge(self.store)
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS integration_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            first_enable = db.execute("SELECT value FROM integration_settings WHERE key='order_web_enabled_v1'").fetchone() is None
        for principal, external in DEMO_BINDINGS.items():
            # 正式启用仅限事先定义的两个虚构账号，不将访客/VIP归到他人名下。
            with self.store.connect() as db:
                existing = db.execute("SELECT external_user_id,enabled FROM module_identity_bindings WHERE principal_id=?", (principal,)).fetchone()
            if existing is None or first_enable and existing == (external, 0):
                identities.bind(principal, external, enabled=True)
            elif existing[0] != external:
                raise ValueError("订单身份映射与预定义演示账号冲突")
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO integration_settings VALUES('order_web_enabled_v1','1')")
        if start_mock_courier:
            simulator = load_order(legacy, "courier_simulator")
            self.courier_server = simulator.create_courier_server(port=0, db_path=Path(self.store.path).parent / "unified-mock-courier.sqlite3")
            self.courier_thread = threading.Thread(target=self.courier_server.serve_forever, daemon=True)
            self.courier_thread.start()
            courier = load_order(legacy, "courier_client").CourierClient(
                base_url=f"http://127.0.0.1:{self.courier_server.server_port}", token=self.courier_server.token)
        generator = make_reply_generator(legacy,
            load_order(legacy, "order_reply.knowledge").KnowledgeRetriever(order_database or legacy / "database/snack_orders_demo.sqlite"),
            model=SharedOrderReplyModel(legacy, self.model) if isinstance(self.model, DeepSeekModel) else None)
        self.order_module = OrderQueryAdapter(legacy, LegacyOrderRepository(order_database or legacy / "database/snack_orders_demo.sqlite"),
            identities, reply_generator=generator, courier=courier)
        self.service.modules.register(self.order_module, self.order_module.verify)
        routing = RoutingClarification()
        self.service.modules.register(routing, routing.verify)
        self.response_timeout_seconds = 2 * getattr(self.model, "timeout", 45) + (self.reply_generator.timeout_seconds if self.reply_generator else 0) + 15
        self.lock, self.slots, self.inflight = threading.Lock(), threading.BoundedSemaphore(4), {}
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS web_contexts(id TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL, touched REAL NOT NULL)")

    def bootstrap(self):
        return {"application": "snack-customer-service", "provider": "DeepSeek",
            "model_configured": bool(getattr(self.model, "api_key", False)), "model": getattr(self.model, "model", None),
            "request_timeout_seconds": getattr(self.model, "timeout", 45),
            "response_timeout_seconds": self.response_timeout_seconds,
            "is_demo": True, "reference_time": self.catalog.data["manifest"]["reference_time"],
            "modules": list(self.service.modules.module_ids), "order_profiles": list(DEMO_BINDINGS),
            "logistics_simulated": self.order_module.courier is not None,
            "model_usage": dict(getattr(self.model, "usage", {})),
            "profiles": [{"id": key, "label": value} for key, value in PROFILE_LABELS.items()]}

    def close(self):
        if self.courier_server:
            self.courier_server.shutdown()
            self.courier_server.server_close()
            self.courier_thread.join(timeout=3)
            self.courier_server = None

    def profile(self, cid, owner):
        with self.store.connect() as db:
            row = db.execute("SELECT profile FROM web_contexts WHERE id=? AND owner=?", (cid, owner)).fetchone()
        return row[0] if row else None

    def conversations(self, owner):
        with self.store.connect() as db:
            rows = db.execute("SELECT s.state,s.history,c.profile FROM sessions s LEFT JOIN web_contexts c ON c.id=s.id AND c.owner=s.owner "
                "WHERE s.owner=? ORDER BY COALESCE(c.touched,0) DESC,s.rowid DESC LIMIT 30", (owner,)).fetchall()
        result = []
        for raw_state, raw_history, profile in rows:
            state, history = json.loads(raw_state), json.loads(raw_history)
            first = next((h["content"] for h in history if h["role"] == "user"), "新的咨询")
            result.append({"id": state["conversation_id"], "title": first[:32], "revision": state["revision"],
                "demo_user_id": profile or "U_GUEST"})
        return {"conversations": result}

    def history(self, cid, owner):
        state, messages = self.store.session(cid, owner)
        return {"conversation_id": cid, "revision": state["revision"], "history": messages,
            "demo_user_id": self.profile(cid, owner) or "U_GUEST"}

    def chat(self, request, owner, profile):
        if profile not in PROFILE_LABELS:
            raise ServiceFailure("invalid_request", "演示身份无效，请重新选择。")
        try:
            validate_request(request)
        except ContractViolation:
            return self.service.error(request, None, "invalid_request", "输入格式未通过核验，请检查请求内容。", False)
        cid = request["conversation_id"]
        if cid:
            known = self.profile(cid, owner)
            if known is not None and known != profile:
                state, _ = self.store.session(cid, owner)
                return self.service.error(request, state, "invalid_request", "这段对话的演示身份已固定，请开启新对话再切换身份。", False)
        key = (owner, cid or "new", request["request_id"])
        fingerprint = hashlib.sha256(json.dumps([request, profile], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            running = self.inflight.get(key)
            if running:
                if running[0] != fingerprint:
                    previous, _ = self.store.session(cid, owner) if cid else (None, [])
                    return self.service.error(request, previous, "request_id_conflict", "相同请求编号对应了不同内容，请使用新的请求编号。", False)
                future, leader = running[1], False
            else:
                if not self.slots.acquire(blocking=False):
                    raise ServiceFailure("data_unavailable", "客服正在处理其他请求，请稍后重试。", True)
                future, leader = Future(), True
                self.inflight[key] = (fingerprint, future)
        if not leader:
            return future.result(timeout=self.response_timeout_seconds + 10)
        try:
            response = asyncio.run(self.service.handle(request, {"owner_id": owner, "user_id": profile, "channel": "web"}))
            if response["status"] != "error":
                with self.store.connect() as db:
                    db.execute("INSERT INTO web_contexts VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET touched=excluded.touched",
                        (response["conversation_id"], owner, profile, time.time()))
            future.set_result(response)
            return response
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.inflight.pop(key, None)
            self.slots.release()


def make_server(app, host="127.0.0.1", port=8787):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("演示服务仅允许监听本机地址")
    class Handler(BaseHTTPRequestHandler):
        server_version = "SnackDemo/1.0"

        def log_message(self, *args):
            pass  # 不记录消息、请求头或供应商响应。

        def owner(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie["snack_client"].value if "snack_client" in cookie else ""
            except Exception:
                value = ""
            if not re.fullmatch(r"[a-f0-9]{48}", value):
                value = secrets.token_hex(24)
                self.new_cookie = value
            return "web_" + value

        def allowed(self):
            authority = self.headers.get("Host", "")
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            return authority in allowed_hosts and (origin is None or origin in {"http://" + h for h in allowed_hosts})

        def respond(self, value, status=200, mime="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if getattr(self, "new_cookie", None):
                self.send_header("Set-Cookie", f"snack_client={self.new_cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # 停止等待不会撤销已完成的服务端提交。

        def dispatch_error(self, failure, status=400):
            self.respond({"error": {"code": failure.code, "message": failure.public_message, "retryable": failure.retryable}}, status)

        def do_GET(self):
            if not self.allowed():
                self.respond({"error": {"message": "请求来源无效。"}}, 403)
                return
            owner = self.owner()
            url = urlsplit(self.path)
            try:
                if url.path in STATIC:
                    file, mime = STATIC[url.path]
                    self.respond((ROOT / "web" / file).read_bytes(), mime=mime)
                elif url.path == "/api/bootstrap":
                    self.respond(app.bootstrap())
                elif url.path == "/api/conversations":
                    self.respond(app.conversations(owner))
                elif url.path == "/api/conversation":
                    cid = parse_qs(url.query).get("id", [""])[0]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid):
                        raise ServiceFailure("invalid_request", "会话编号无效。")
                    self.respond(app.history(cid, owner))
                elif url.path == "/api/request":
                    params = parse_qs(url.query)
                    scope, rid = params.get("scope", [""])[0], params.get("id", [""])[0]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scope) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rid):
                        raise ServiceFailure("invalid_request", "请求编号无效。")
                    cached = app.store.cached(owner, scope, rid)
                    self.respond({"response": json.loads(cached[1]) if cached else None})
                else:
                    self.respond({"error": {"message": "页面不存在。"}}, 404)
            except ServiceFailure as exc:
                self.dispatch_error(exc, 404)
            except (OSError, ValueError):
                self.respond({"error": {"message": "页面或会话暂时无法读取。"}}, 500)

        def do_POST(self):
            if not self.allowed():
                self.respond({"error": {"message": "请求来源无效。"}}, 403)
                return
            owner = self.owner()
            if self.path != "/api/chat":
                self.respond({"error": {"message": "接口不存在。"}}, 404)
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ServiceFailure("invalid_request", "请求必须使用 JSON 格式。")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 32000:
                    raise ServiceFailure("invalid_request", "输入为空或过长。")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                response = app.chat(request, owner, self.headers.get("X-Demo-User", "U_GUEST"))
                self.respond(response)
            except ServiceFailure as exc:
                self.dispatch_error(exc, 429 if exc.retryable else 400)
            except (ValueError, UnicodeError):
                self.respond({"error": {"message": "输入格式无效。", "retryable": False}}, 400)
            except Exception:
                self.respond({"error": {"message": "服务暂时不可用，请重试原消息。", "retryable": True}}, 503)

    server = LocalHTTPServer((host, port), Handler)
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description="启动本地零食客服对话界面")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--database")
    args = parser.parse_args()
    load_env()
    app = WebApp(args.database, start_mock_courier=True)
    try:
        server = make_server(app, port=args.port)
    except Exception:
        app.close()
        raise
    print(f"客服页面 http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
