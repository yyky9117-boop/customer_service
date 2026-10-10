"""第三步的命令行入口；网页和 HTTP API 留到第四步。"""
import argparse
import asyncio
import json
import uuid
import sys
from pathlib import Path

from .catalog import Catalog, ROOT, instant
from .model import DeepSeekModel, load_env
from .module_interface import ModuleContext
from .needs import merge_patch
from .recommendation import RecommendationModule
from .reply import ReplyGenerator
from .service import ChatService, SessionStore


async def run(args):
    load_env()
    model = DeepSeekModel()
    if args.check_config:
        print(json.dumps({"provider": "DeepSeek", "model": model.model, "base_url": model.base_url,
            "key_configured": bool(model.api_key), "live_call_executed": False}, ensure_ascii=False, indent=2))
        return
    catalog = Catalog.load()
    now = instant(args.now or catalog.data["manifest"]["reference_time"])
    trusted = {"owner_id": "local_demo", "user_id": args.demo_user, "channel": "web"}
    module = RecommendationModule(catalog, model)
    if args.structured_input:
        example = json.loads(Path(args.structured_input).read_text(encoding="utf-8"))
        context = ModuleContext("conv_offline", "req_offline", "turn_offline", 0, example["message"], {}, (), None, now, trusted)
        state = merge_patch({}, example["patch"], context)
        result = module.recommend(state, context)
        output = {"mode": "structured_input_offline", "live_model_used": False, "status": result.status,
            "content": result.content, "cards": result.cards, "clarification": result.clarification}
        if args.trace:
            output["debug"] = result.debug
    else:
        if not args.message:
            raise SystemExit("请提供 --message 或 --structured-input。")
        store = SessionStore(args.database or ROOT / ".local" / "sessions.sqlite3")
        service = ChatService(module, store, clock=lambda: now, reply_generator=ReplyGenerator(model, catalog))
        request = {"schema_version": "1.0", "conversation_id": args.conversation_id, "request_id": args.request_id or "req_" + uuid.uuid4().hex,
            "expected_revision": args.revision, "message": args.message, "selected_reference": None}
        output = await service.handle(request, trusted)
        if args.trace:
            output = {"response": output, "debug": store.diagnostics(request)}
    print(json.dumps(output, ensure_ascii=False, indent=2))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="零食客服推荐核心")
    parser.add_argument("--message")
    parser.add_argument("--conversation-id")
    parser.add_argument("--request-id")
    parser.add_argument("--revision", type=int, default=0)
    parser.add_argument("--demo-user", choices=["U_GUEST", "U_NEW", "U_RETURN", "U_VIP", "U_UNKNOWN", "U_LOCKED"], default="U_GUEST")
    parser.add_argument("--now", help="有时区 ISO 时间；默认演示数据固定时间")
    parser.add_argument("--database")
    parser.add_argument("--structured-input", help="显式离线结构化输入；不调用模型")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
