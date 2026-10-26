"""A local-only stdlib HTTP app. Run python -m demo from the project folder."""
import argparse
import asyncio
import json
import logging
import os
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from pydantic import ValidationError

from .config import Config, load_model_env
from .graphiti_extension import Pipeline
from .model import DemoError, require
from .service import Service
from .store import Store
from .mobile_gateway import MobileGateway
from .mobile_auth import MobileAuth, configured_web_origins
from .mobile_speech import transcribe

ROOT = Path(__file__).resolve().parent.parent


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-env", help="Optional existing model-only env file; KD_* database settings remain local to Demo.")
    args = parser.parse_args()
    if args.model_env:
        load_model_env(args.model_env)
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
    host, port = os.getenv("KD_HOST", "127.0.0.1"), int(os.getenv("KD_PORT", "8096"))
    require(host in {"127.0.0.1", "localhost"}, "local_only", "此 Demo 无多人鉴权，仅支持本机监听。")
    store = Store(Config.from_env())
    store.initialize()
    loop = asyncio.new_event_loop()
    pipeline = Pipeline(store.config)
    service = Service(store, pipeline)
    mobile = MobileGateway(store, pipeline)
    mobile_auth = MobileAuth(store)
    web_origins = configured_web_origins()
    stop = asyncio.Event()
    worker = loop.create_task(pipeline.worker(store, stop))
    thread = Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AtlasKnowledgeDemo/0.1"
        protocol_version = "HTTP/1.1"
        timeout = 15

        def log_message(self, *args):
            pass

        def send_json(self, result, status=200):
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                path = urlparse(self.path)
                if path.path == '/api/mobile/session':
                    mobile_auth.rate_limit(('identity', self.client_address[0]), 120)
                    identity = mobile_auth.authenticate(self.headers.get('Authorization'))
                    data = mobile_auth.session(identity, self.headers.get('X-Workspace-ID', ''))
                    self.send_json({'ok': True, 'data': data})
                    return
                params = parse_qs(path.query)
                offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [25])[0])
                revision = int(params["revision"][0]) if "revision" in params else None
                oid = params.get("id", [""])[0]
                if path.path == "/api/health":
                    data = {"database": "ready", "model_configured": bool(os.getenv("LLM_API_KEY")), "agent_framework": "pydantic-ai", **store.health()}
                elif path.path == "/api/types":
                    data = store.types(offset, limit, revision, params.get("include_archived", ["false"])[0] == "true")
                elif path.path == "/api/objects":
                    data = store.objects(params.get("kind", [""])[0], offset, limit, revision, params.get("include_archived", ["false"])[0] == "true",params.get('status',['all'])[0])
                elif path.path == '/api/batches':
                    data=store.batches(service.scope,offset,limit,params.get('include_closed',['false'])[0]=='true')
                elif path.path == '/api/batch':
                    service.scope.check(store,'read')
                    data=store.read(lambda tx:store._batch(tx,oid,service.scope))
                elif path.path == "/api/review-impact":
                    data = service.review_impact(oid, offset, limit)
                elif path.path == "/api/detail":
                    data = store.detail(oid, offset, limit, revision, params.get("history", ["false"])[0] == "true")
                    if data["history"]:
                        data["mutations"] = store.mutation_history(oid)
                elif path.path == "/api/neighbors":
                    data = store.neighbors(oid, offset, limit, revision, int(params.get("depth", [0])[0]), int(params.get("max_depth", [4])[0]))
                elif path.path == "/api/graph":
                    service.scope.check(store, "read")
                    data = store.graph(revision)
                elif path.path == "/api/sources":
                    data = store.sources(oid, offset, limit)
                elif path.path == "/api/audit":
                    data = store.audit(oid, offset, limit)
                elif path.path == "/api/messages":
                    data = store.messages()
                elif path.path == "/api/draft":
                    data = service.public_draft(oid)
                elif path.path == "/api/conversation":
                    state = store.conversation(service.scope)
                    data = {"focus_refs": state["focus_refs"], "pending": state.get("pending"), "approval": state.get("approval")}
                elif path.path == "/api/import":
                    data = service.bulk.get(oid)
                else:
                    routes = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/review.js": ("review.js", "text/javascript"), "/star.js": ("star.js", "text/javascript"), "/bulk.js": ("bulk.js", "text/javascript"), "/vendor/g6-5.1.1.min.js": ("vendor/g6-5.1.1.min.js", "text/javascript"), "/vendor/G6-LICENSE.txt": ("vendor/G6-LICENSE.txt", "text/plain"), "/style.css": ("style.css", "text/css")}
                    routes['/pending.js']=('pending.js','text/javascript')
                    require(path.path in routes, "not_found", "页面不存在。", 404)
                    name, mime = routes[path.path]
                    body = (ROOT / "web" / name).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mime + "; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_json({"ok": True, "data": data})
            except Exception as exc:
                self.error_response(exc)

        def do_POST(self):
            try:
                self.connection.settimeout(15)
                mobile_paths = {'/api/mobile/execute', '/api/mobile/transcribe', '/api/mobile/logout'}
                is_mobile = self.path in mobile_paths
                identity = None
                context = None
                if is_mobile:
                    mobile_auth.rate_limit(('identity', self.client_address[0]), 1200)
                    identity = mobile_auth.authenticate(self.headers.get('Authorization'))
                    if self.path != '/api/mobile/logout':
                        context, _ = mobile_auth.authorize(identity, self.headers.get('X-Workspace-ID', ''))
                        mobile_auth.rate_limit(('requests', identity.actor), 90)
                origin = self.headers.get("Origin")
                allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
                if is_mobile:
                    allowed_origins |= web_origins
                require(origin is None or origin in allowed_origins, "invalid_origin", "请求来源不允许。", 403)
                require(self.headers.get("Content-Type", "").split(";")[0] == "application/json", "invalid_content_type", "请发送 JSON 请求。")
                size = int(self.headers.get("Content-Length", "0"))
                max_size = (2800000 if self.path == '/api/mobile/transcribe' else
                            512000 if is_mobile or self.path == '/api/batch-review' else 65536)
                require(0 < size <= max_size, "invalid_body", "请求大小不正确。")
                body = json.loads(self.rfile.read(size))
                require(isinstance(body, dict), "invalid_body", "请求必须为 JSON 对象。")
                if self.path == '/api/mobile/execute':
                    require(set(body) == {'operation', 'payload'}, 'invalid_input', '请求格式无效。')
                    data = asyncio.run_coroutine_threadsafe(mobile.dispatch({
                        'context': context, 'operation': body['operation'], 'payload': body['payload']}), loop).result(timeout=45)
                elif self.path == '/api/mobile/transcribe':
                    mobile_auth.rate_limit(('speech', identity.actor), 15)
                    data = transcribe(body, actor=context['actor'], workspace=context['tenant'])
                elif self.path == '/api/mobile/logout':
                    require(not body, 'invalid_input', '退出请求参数无效。')
                    data = mobile_auth.logout(identity)
                elif self.path == "/api/chat":
                    require(set(body) <= {"message", "request_id", "context_ref"} and "message" in body, "invalid_body", "消息格式错误。")
                    require("request_id" not in body or isinstance(body["request_id"], str) and 0 < len(body["request_id"]) <= 100, "invalid_body", "请求标识格式错误。")
                    future = asyncio.run_coroutine_threadsafe(service.chat(body["message"], body.get("request_id"), body.get("context_ref")), loop)
                    try:
                        data = future.result(timeout=240)
                    except FutureTimeout as exc:
                        future.cancel()
                        receipt=store.pending_receipt(service.scope,body.get('request_id'),body['message'])
                        if receipt:
                            data={**receipt,'saved_pending':True,'clear_draft':True,'message':'信息已保存为待确认；返回说明超时，可在待确认记录中核对。'}
                        else:
                            raise DemoError("model_timeout", "处理超时，请刷新待确认队列核对结果；原始消息已保留。", 504) from exc
                elif self.path == "/api/confirm":
                    require(set(body) == {"draft_id", "approval_id", "revision"}, "invalid_body", "确认请求缺少服务端授权或预览版本，请刷新预览。")
                    future = asyncio.run_coroutine_threadsafe(service.confirm(body["draft_id"], body["approval_id"], body["revision"]), loop)
                    try:
                        data = future.result(timeout=240)
                    except FutureTimeout as exc:
                        future.cancel()
                        draft = service.public_draft(body["draft_id"])
                        if draft["status"] == "committed":
                            data = {"draft_id": body["draft_id"], "message": "变更已保存。", "warning": "保存完成，生成说明超时；请查看对象与来源核对。"}
                        else:
                            raise DemoError("confirmation_timeout", "确认执行超时，请重试同一确认以核对保存结果。", 504) from exc
                elif self.path == "/api/review-preview":
                    data = asyncio.run_coroutine_threadsafe(service.prepare_review(body), loop).result(timeout=60)
                elif self.path == "/api/cancel-preview":
                    require(set(body) == {"draft_id"}, "invalid_body", "取消预览参数错误。")
                    data = asyncio.run_coroutine_threadsafe(service.cancel_preview(body["draft_id"]), loop).result(timeout=60)
                elif self.path == "/api/resolve":
                    require(set(body) == {"draft_id", "choices"}, "invalid_body", "对象选择格式错误。")
                    data = asyncio.run_coroutine_threadsafe(service.resolve(body["draft_id"], body["choices"]), loop).result(timeout=60)
                elif self.path == "/api/query":
                    data = store.query(body)
                elif self.path == '/api/records':
                    service.scope.check(store,'read')
                    data=store.search_records(body)
                elif self.path == '/api/batch-review':
                    require(set(body)=={'batch_id','item_ids','action'},'invalid_body','请指定批次、所选记录和操作。')
                    data=store.prepare_batch_review(service.scope,body['batch_id'],body['item_ids'],body['action'])
                elif self.path == '/api/batch-apply':
                    require(set(body)=={'token'},'invalid_body','请提供服务端确认凭据。')
                    data=store.apply_batch_review(service.scope,body['token'])
                elif self.path == '/api/index-retry':
                    require(not body,'invalid_body','重试不接受额外参数。')
                    data=store.retry_indexes(service.scope)
                elif self.path == "/api/import":
                    require(set(body) == {"job_id", "action"}, "invalid_body", "导入任务参数无效。")
                    data = asyncio.run_coroutine_threadsafe(service.bulk.control(body["job_id"], body["action"]), loop).result(timeout=60)
                else:
                    raise DemoError("not_found", "接口不存在。", 404)
                self.send_json({"ok": True, "data": data})
            except Exception as exc:
                self.error_response(exc)

        def error_response(self, exc):
            # Rejected POSTs may still have unread bytes. Do not treat that
            # body as another request on a persistent connection.
            self.close_connection = True
            if isinstance(exc, DemoError):
                code, message, status = exc.code, str(exc), exc.status
            elif isinstance(exc, (ValidationError, ValueError, UnicodeError, TypeError)):
                code, message, status = "invalid_input", "结构化参数无效，请核对输入或补充说明。", 400
            else:
                # Do not expose backend exception strings (possible keys/URIs).
                logging.getLogger("demo").error("operation_failed: %s", type(exc).__name__)
                code, message, status = "service_unavailable", "服务执行失败；请检查数据库、模型配置或服务日志。", 503
            self.send_json({"ok": False, "errors": [{"code": code, "message": message}]}, status)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Atlas Knowledge Demo: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        loop.call_soon_threadsafe(stop.set)
        loop.call_soon_threadsafe(worker.cancel)
        asyncio.run_coroutine_threadsafe(mobile.close(), loop).result(timeout=15)
        asyncio.run_coroutine_threadsafe(pipeline.close(), loop).result(timeout=15)
        if service._runtime is not None:
            asyncio.run_coroutine_threadsafe(service.runtime.close(), loop).result(timeout=15)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        store.close()


if __name__ == "__main__":
    run()
