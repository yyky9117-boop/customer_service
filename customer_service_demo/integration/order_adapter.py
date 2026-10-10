"""复用旧订单纯节点；只读业务库，不创建旧会话、不调用物流或人工接口。"""
import importlib.util
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from runtime.errors import ServiceFailure
from runtime.module_interface import ModuleResult
from .legacy_loader import load
from .identifiers import phone_numbers

FIELDS = ("order_id", "tracking_number", "phone_number", "product_name", "query_type")


class LegacyOrderRepository:
    def __init__(self, db_path):
        self.path = Path(db_path).resolve()

    def snapshot(self, user_id, *, phone_number=None):
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                if not db.execute("SELECT 1 FROM users WHERE user_id=? AND is_demo=1", (user_id,)).fetchone():
                    raise ServiceFailure("invalid_request", "订单身份绑定无效，请核对演示数据。")
                orders = []
                query = "SELECT * FROM orders WHERE user_id=?"
                values = [user_id]
                if phone_number is not None:
                    query += " AND user_phonenumber=?"
                    values.append(phone_number)
                for row in db.execute(query + " ORDER BY order_id", values):
                    order = dict(row)
                    order["items"] = [dict(item) for item in db.execute(
                        "SELECT sku_id,item_title,quantity FROM order_items WHERE order_id=? ORDER BY item_id", (order["order_id"],))]
                    order["shipments"] = []
                    for row in db.execute("SELECT * FROM shipments WHERE order_id=? ORDER BY shipment_id", (order["order_id"],)):
                        shipment = dict(row)
                        # 存量轨迹不是本轮快递接口返回；统一接入不能拿它假装实时查询。
                        shipment.update(logistics_status="unknown", latest_status_desc="", estimated_delivery_at=None,
                            eta_source=None, tracking_events=[], data_source="not_queried", updated_at=None)
                        order["shipments"].append(shipment)
                    orders.append(order)
                return orders
        except (sqlite3.Error, OSError):
            raise ServiceFailure("data_unavailable", "订单演示数据库暂时无法读取，本轮未保存。", True) from None


class OrderQueryAdapter:
    module_id = "order_query"

    def __init__(self, project_root, repository, identities, reply_generator=None, courier=None):
        self.repository, self.identities = repository, identities
        self.reply_generator, self.courier = reply_generator, courier
        self.nodes = {}
        for name in ("normalize", "query", "render"):
            path = Path(project_root).resolve() / "nodes" / (name + ".py")
            spec = importlib.util.spec_from_file_location("_snack_integration_order_" + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.nodes[name] = module.main

    async def handle(self, context):
        user = self.identities.resolve(context.trusted_context)
        message = context.message
        params = {key: "" for key in FIELDS}
        upper = message.upper()
        order_numbers = set(re.findall(r"(?<![A-Z0-9-])SN-DEMO-\d{4}(?![A-Z0-9-])", upper))
        tracking_numbers = set(re.findall(r"(?<![A-Z0-9-])DEMO-TRACK-[A-Z0-9]+(?![A-Z0-9-])", upper))
        phones = phone_numbers(message)
        multiple_phones = not order_numbers and not tracking_numbers and len(phones) > 1
        if len(order_numbers) > 1 or len(tracking_numbers) > 1 or multiple_phones:
            ask = {"question": "这次提供了多个手机号，请先指定一个下单手机号，我再查询对应订单。" if multiple_phones else "这次出现了多个订单或运单编号，请先指定要查询的一笔；查完可以继续下一笔。", "asked_fields": ["phone_number" if multiple_phones else "order_id"], "group_id": None}
            state = {"params": params, "selected_order_id": None, "candidate_order_ids": [], "pending_clarification": ask,
                     "last_result_type": "need_more_info", "source": "legacy_readonly"}
            return ModuleResult("needs_clarification", ask["question"], (), ask, state,
                {"legacy_result": {"orders": [], "candidates": []}, "live_model_used": False,
                 "live_logistics_queried": False, "human_ticket_created": False})
        order = re.search(r"(?<![A-Z0-9-])SN-DEMO-\d{4}(?![A-Z0-9-])", upper)
        tracking = re.search(r"(?<![A-Z0-9-])DEMO-TRACK-[A-Z0-9]+(?![A-Z0-9-])", upper)
        phone = phones[0] if phones else ""
        params.update(order_id=order.group(0) if order else "", tracking_number=tracking.group(0) if tracking else "",
            phone_number=phone, query_type="general")
        if not phone:
            raw_phone = re.search(r"(?:手机号|手机号码|电话|手机)\s*[：:]?\s*(\d+)", message)
            if raw_phone:
                params["phone_number"] = raw_phone.group(1)
        if any(w in message for w in ("什么时候到", "啥时候到", "多久到", "几号到", "送到", "送达")):
            params["query_type"] = "delivery_time"
        elif any(w in message for w in ("发货", "发了没", "发了吗", "发出")):
            params["query_type"] = "shipping"
        elif any(w in message for w in ("物流", "快递", "运单", "到哪", "签收", "包裹")):
            params["query_type"] = "logistics"
        # 不完整的新号码也必须作为本轮条件，不能悄悄继续查询旧订单。
        if not order:
            raw_order = re.search(r"(?:订单号\s*[：:#]?\s*|\b)((?:SN-DEMO-|ORDER-|DD)[A-Z0-9-]+)", upper)
            if raw_order:
                params["order_id"] = raw_order.group(1)
        if not tracking:
            raw_tracking = re.search(r"(DEMO-TRACK-[A-Z0-9-]*)", upper) or re.search(r"(?:运单号|快递单号|物流单号)\s*[：:#]?\s*([A-Z0-9-]+)", upper)
            if raw_tracking:
                params["tracking_number"] = raw_tracking.group(1)
        if not params["order_id"] and not params["tracking_number"]:
            for product in ("牛肉干", "牛肉粒", "薯片", "坚果", "腰果", "核桃", "辣条", "饼干", "曲奇", "零食组合装"):
                if product in message:
                    params["product_name"] = "饼干" if product == "曲奇" else product
                    break
        # 新完整号码优先，不能被上一轮手机号条件误挡。
        if params["order_id"] or params["tracking_number"]:
            params["phone_number"] = ""
            params["product_name"] = ""
        candidates = context.module_state.get("candidate_order_ids", [])
        choice = re.search(r"(?:第)?([一二三四五六七八九1-9])(?:笔|单|个|条)", message)
        invalid_choice = False
        if choice and not any(params[k] for k in ("order_id", "tracking_number", "phone_number", "product_name")) and candidates:
            index = "一二三四五六七八九".find(choice.group(1)) if not choice.group(1).isdigit() else int(choice.group(1)) - 1
            if index < len(candidates):
                params["order_id"] = candidates[index]
                if params["query_type"] == "general":
                    params["query_type"] = context.module_state.get("params", {}).get("query_type", "general")
            else:
                invalid_choice = True
        selected = context.module_state.get("selected_order_id")
        prior = context.module_state.get("params", {})
        if context.module_state.get("pending_clarification") and params["query_type"] == "general":
            params["query_type"] = prior.get("query_type", "general")
        if phone and not params["order_id"] and not params["tracking_number"] and context.module_state.get("pending_clarification"):
            params["product_name"] = params["product_name"] or prior.get("product_name", "")
        reset = any(term in message for term in ("另一笔", "另一个", "换个订单", "换一笔", "重新查询"))
        if not invalid_choice and not any(params[key] for key in ("order_id", "tracking_number", "phone_number", "product_name")) and not reset:
            params["order_id"] = selected or ""
        normalized = await self.nodes["normalize"](SimpleNamespace(params={"extracted": params, "user_id": user,
            "USER_INPUT": message, "context_json": "{}"}))
        normalized_params = json.loads(normalized["params_json"])
        # 用独立订单数据库模拟外部数据源；手机号在 SQL 查询中参与筛选。
        lookup_phone = normalized_params["phone_number"]
        source_orders = (self.repository.snapshot(user, phone_number=lookup_phone) if lookup_phone
                         else self.repository.snapshot(user))
        query_output = await self.nodes["query"](SimpleNamespace(params={"params_json": normalized["params_json"],
            "data_json": json.dumps(source_orders, ensure_ascii=False)}))
        queried = json.loads(query_output["query_result_json"])
        if invalid_choice:
            queried.update(result_type="need_more_info", orders=[], candidates=[])
        queried = await self.fetch_logistics(queried)
        query_output["query_result_json"] = json.dumps(queried, ensure_ascii=False)
        if queried["result_type"] in {"query_failed", "auth_required"}:
            raise ServiceFailure("data_unavailable", "订单查询结果未通过核验，本轮未保存。", True)
        rendered = await self.nodes["render"](SimpleNamespace(params=query_output))
        result = json.loads(rendered["result_json"])
        if result["next_action"] == "request_human" or any(w in message for w in ("人工", "退款", "退货", "赔偿", "投诉")):
            # 当前界面没有人工入口：不能复用旧界面“点击联系人工”的承诺。
            result["next_action"] = "none"
            result["human_transfer"] = {"requested": False, "status": "not_connected", "ticket_id": None}
            result["human_request"] = None
        kind = result["result_type"]
        if kind == "query_failed":
            raise ServiceFailure("data_unavailable", "订单状态暂时无法核验，本轮未保存。", True)
        pending = None
        if kind in {"need_more_info", "multiple_orders", "narrow_search"}:
            status = "needs_clarification"
            question = query_output["selection_question"] if kind == "multiple_orders" else "请提供商城订单号、快递运单号或下单手机号。"
            pending = {"question": question, "asked_fields": ["order_id"], "group_id": None}
            content = question
        else:
            status = "no_match" if kind == "not_found" else "completed"
            content = result["reply"]
        if self.reply_generator:
            content, trace = await self.reply_generator.generate(result, message, list(context.history))
            result["reply_status"] = trace
        if kind == "multiple_orders" and queried.get("total_count", 0) > len(result.get("candidates", [])):
            content += (f"\n共匹配到{queried['total_count']}笔订单，当前展示最近{len(result['candidates'])}笔；"
                        "也可以输入完整订单号继续查询。")
        if self.courier is None and any(term in message for term in ("物流", "快递", "到哪", "什么时候到", "运单")):
            content += " 当前统一接入仅查询订单与发货状态，尚未调用实时物流接口。"
        if any(w in message for w in ("人工", "退款", "退货", "赔偿", "投诉")) or result.get("reason") in {"unknown_shipping_status", "logistics_exception", "missing_shipment", "tracking_stalled"}:
            content += " 这个问题需要人工核实；本演示尚未接入人工，也不会执行退款、退货或催单。"
        if self.courier and queried.get("courier_calls"):
            content += "（物流为本机模拟快递接口返回。）"
        if pending:
            # 将实际生成的候选列表与服务端追问保存为同一份内容。
            if result.get("reason") == "invalid_phone":
                content = "这个手机号格式不完整或不正确，请提供11位下单手机号；也可以直接发送商城订单号。"
                queried_candidates = []
            elif invalid_choice:
                content = "这次列出的订单没有该序号，请回复列表里的完整商城订单号。"
                queried_candidates = candidates
            else:
                queried_candidates = [o["order_id"] for o in result.get("candidates", [])]
            pending = {**pending, "question": content}
        else:
            queried_candidates = [o["order_id"] for o in result.get("candidates", [])]
        state = {"params": {key: normalized_params[key] for key in FIELDS},
            "selected_order_id": result["context"].get("selected_order_id"),
            "candidate_order_ids": queried_candidates,
            "pending_clarification": pending, "last_result_type": kind, "source": "legacy_readonly"}
        return ModuleResult(status, content, (), pending, state,
            {"engine": "legacy_order_readonly", "legacy_result": result, "live_model_used": result.get("reply_status", {}).get("mode") == "deepseek_grounded_reply",
             "database_lookup": {"type": "phone_number" if lookup_phone else "order_or_other_identifier", "matched_phone_rows": len(source_orders) if lookup_phone else None},
             "live_logistics_queried": bool(queried.get("courier_calls")), "courier_calls": queried.get("courier_calls", []),
             "reply_trace": result.get("reply_status"), "human_ticket_created": False})

    async def fetch_logistics(self, queried):
        import asyncio
        queried["courier_calls"] = []
        if not self.courier or queried["result_type"] != "success" or queried["query_type"] == "shipping":
            return queried
        for order in queried.get("orders", []):
            if order["order_status"] in ("pending_payment", "cancelled") or order["shipping_status"] not in ("shipped", "partially_shipped"):
                continue
            for shipment in order.get("shipments", []):
                if not shipment.get("tracking_number"):
                    continue
                parcel, trace = await asyncio.to_thread(self.courier.lookup, shipment["tracking_number"], shipment.get("carrier_code", "mock_express"))
                trace.update(order_id=order["order_id"], shipment_id=shipment["shipment_id"])
                queried["courier_calls"].append(trace)
                if parcel is None:
                    shipment.update(logistics_status="query_failed", data_source="courier_api_failed", latest_status_desc="",
                                    estimated_delivery_at=None, eta_source=None, tracking_events=[])
                else:
                    shipment.update(logistics_status=parcel["status"], latest_status_desc=parcel["latest_description"],
                        estimated_delivery_at=parcel["estimated_delivery_at"], eta_source=parcel["eta_source"],
                        data_source="courier_http", updated_at=trace["called_at"], simulation_time=parcel["simulation_time"],
                        scenario=parcel["scenario"], tracking_events=[{"status_time": e["time"], "status_desc": e["description"], "location": e["location"]} for e in parcel["events"]])
        return queried

    def verify(self, result, context):
        user = self.identities.resolve(context.trusted_context)
        snapshot = {o["order_id"]: o for o in self.repository.snapshot(user)}
        owned = set(snapshot)
        legacy = result.debug.get("legacy_result", {})
        returned = {o["order_id"] for o in legacy.get("orders", []) + legacy.get("candidates", [])}
        if (not returned <= owned or result.cards or result.next_module_state.get("selected_order_id") not in owned | {None}
                or not set(result.next_module_state.get("candidate_order_ids", [])) <= owned):
            raise ServiceFailure("invalid_model_output", "订单归属未通过核验，本轮未保存。", True)
        for order in legacy.get("orders", []) + legacy.get("candidates", []):
            source = snapshot[order["order_id"]]
            if any(order.get(key) != source.get(key) for key in ("order_status", "shipping_status", "amount_fen", "created_at", "items")):
                raise ServiceFailure("invalid_model_output", "订单事实发生变化，本轮未保存，请重新查询。", True)
        if result.status == "completed" and len(legacy.get("orders", [])) != 1:
            raise ServiceFailure("invalid_model_output", "订单结果数量未通过核验，本轮未保存。", True)
