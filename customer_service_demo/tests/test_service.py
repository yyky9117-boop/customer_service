import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.errors import InvalidModelOutput, ServiceFailure
from runtime.recommendation import RecommendationModule
from runtime.service import ChatService, SessionStore
from core_helpers import NOW, ScriptedModel, WorkspaceTemp, budget, catalog, constraint, patch, update


def request(rid="req_test", cid=None, revision=0, message="测试需求", reference=None):
    return {"schema_version": "1.0", "conversation_id": cid, "request_id": rid,
        "expected_revision": revision, "message": message, "selected_reference": reference}


def normal_patch():
    return patch([update(constraints=[constraint("package", "wrapped")])])


class GateModel(ScriptedModel):
    def __init__(self):
        super().__init__(normal_patch(), normal_patch())
        self.ready = asyncio.Event()

    async def extract(self, payload, repair=None):
        result = await super().extract(payload, repair)
        if len(self.calls) == 2:
            self.ready.set()
        await self.ready.wait()
        return result


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / "session.sqlite3")

    def service(self, model=None):
        self.model = model or ScriptedModel(normal_patch())
        self.module = RecommendationModule(catalog(), self.model)
        return ChatService(self.module, self.store, clock=lambda: NOW)

    async def test_success_commits_one_user_and_assistant_message(self):
        service = self.service()
        result = await service.handle(request())
        self.assertEqual(result["status"], "completed")
        state, history = self.store.session(result["conversation_id"], "local_demo")
        self.assertEqual(state["revision"], 1)
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(state["module_states"]["product_recommendation"]["last_presentation"]["message_id"], result["message"]["id"])

    async def test_idempotent_new_request_replays_same_response(self):
        service = self.service()
        original = await service.handle(request())
        replay = await service.handle(request())
        self.assertEqual(original, replay)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.store.session(original["conversation_id"], "local_demo")[1]), 2)

    async def test_same_request_id_different_payload_conflict(self):
        service = self.service()
        await service.handle(request())
        result = await service.handle(request(message="另一段内容"))
        self.assertEqual(result["error"]["code"], "request_id_conflict")
        self.assertEqual(len(self.model.calls), 1)

    async def test_stale_revision_does_not_call_model(self):
        service = self.service()
        first = await service.handle(request())
        result = await service.handle(request("req_second", first["conversation_id"], 0))
        self.assertEqual(result["error"]["code"], "revision_conflict")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(len(self.model.calls), 1)

    async def test_existing_success_replayed_even_after_later_revision(self):
        service = self.service(ScriptedModel(normal_patch(), patch()))
        first = await service.handle(request())
        req = request("req_second", first["conversation_id"], 1)
        second = await service.handle(req)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(await service.handle(request()), first)
        self.assertEqual(await service.handle(req), second)

    async def test_invalid_model_repaired_once_then_success(self):
        service = self.service(ScriptedModel({"garbage": True}, normal_patch()))
        result = await service.handle(request())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.model.calls), 2)
        self.assertTrue(self.model.calls[1]["repair"])
        self.assertEqual(self.store.diagnostics(request())["model_attempts"], 2)

    async def test_invalid_model_twice_has_no_session_or_cards(self):
        service = self.service(ScriptedModel({}, {}))
        result = await service.handle(request())
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertIsNone(result["conversation_id"])
        self.assertEqual(result["message"]["cards"], [])
        self.assertIsNone(self.store.cached("local_demo", "new", "req_test"))

    async def test_timeout_does_not_commit_and_same_id_can_retry(self):
        service = self.service(ScriptedModel(ServiceFailure("model_timeout", "超时", True), normal_patch()))
        failed = await service.handle(request())
        self.assertEqual(failed["revision"], 0)
        self.assertIsNone(failed["conversation_id"])
        success = await service.handle(request())
        self.assertEqual(success["status"], "completed")
        self.assertEqual(success["revision"], 1)

    async def test_failed_followup_preserves_state_history_and_last_reference(self):
        service = self.service(ScriptedModel(normal_patch(), {}, {}))
        first = await service.handle(request())
        before = self.store.session(first["conversation_id"], "local_demo")
        failed = await service.handle(request("req_second", first["conversation_id"], 1))
        self.assertEqual(failed["revision"], 1)
        self.assertEqual(self.store.session(first["conversation_id"], "local_demo"), before)

    async def test_new_conversation_isolated_from_old_constraints(self):
        service = self.service(ScriptedModel(normal_patch(), patch()))
        first = await service.handle(request())
        second = await service.handle(request("req_another"))
        self.assertEqual(second["status"], "needs_clarification")
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

    async def test_other_owner_cannot_read_session_or_references(self):
        service = self.service()
        first = await service.handle(request())
        result = await service.handle(request("req_second", first["conversation_id"], 1), {"owner_id": "another_owner"})
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertFalse(result["message"]["cards"])

    async def test_forged_card_reference_rejected_before_model(self):
        service = self.service()
        first = await service.handle(request())
        reference = {"message_id": first["message"]["id"], "plan_id": "plan_fake", "sku_id": None}
        result = await service.handle(request("req_second", first["conversation_id"], 1, reference=reference))
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(len(self.model.calls), 1)

    async def test_real_card_reference_resolved_and_repriced(self):
        # 结构化脚本模拟模型解析已选卡片；不是指代语义准确率测试。
        initial = patch([update(constraints=[constraint("package", "wrapped"), constraint("category", "biscuits")])])
        service = self.service(ScriptedModel(initial, patch([update()],
            reference_action="retain_selected",reference_source_text="测试需求")))
        first = await service.handle(request())
        selected = next(card for card in first["message"]["cards"] if any(i["sku_id"] == "P019" for i in card["items"]))
        ref = {"message_id": first["message"]["id"], "plan_id": selected["plan_id"], "sku_id": "P019"}
        second = await service.handle(request("req_second", first["conversation_id"], 1, reference=ref))
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["message"]["cards"][0]["pricing"]["merchandise_payable_fen"], 890)
        self.assertTrue(all(c["items"][0]["sku_id"] == "P019" for c in second["message"]["cards"]))
        self.assertEqual(self.model.calls[1]["payload"]["history"][-1]["role"], "reference")

    async def test_selected_reference_action_is_required_and_repaired(self):
        service = self.service(ScriptedModel(normal_patch(), patch(), patch(
            reference_action="retain_selected", reference_source_text="测试需求")))
        first = await service.handle(request())
        card = first["message"]["cards"][0]
        ref = {"message_id": first["message"]["id"], "plan_id": card["plan_id"], "sku_id": card["items"][0]["sku_id"]}
        second = await service.handle(request("req_second", first["conversation_id"], 1, reference=ref))
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.model.calls), 3)
        self.assertTrue(self.model.calls[-1]["repair"])

    async def test_replace_selected_excludes_previous_sku(self):
        service = self.service(ScriptedModel(normal_patch(), patch(
            reference_action="replace_selected", reference_source_text="测试需求")))
        first = await service.handle(request())
        card = first["message"]["cards"][0]
        sku = card["items"][0]["sku_id"]
        ref = {"message_id": first["message"]["id"], "plan_id": card["plan_id"], "sku_id": sku}
        second = await service.handle(request("req_second", first["conversation_id"], 1, reference=ref))
        self.assertEqual(second["status"], "completed")
        self.assertTrue(all(i["sku_id"] != sku for c in second["message"]["cards"] for i in c["items"]))

    async def test_explicit_alternatives_release_previous_selection(self):
        service = self.service(ScriptedModel(normal_patch(), patch(
            reference_action="retain_selected", reference_source_text="测试需求"), patch(
            reference_action="recommend_alternatives", reference_source_text="测试需求")))
        first = await service.handle(request())
        card = first["message"]["cards"][0]
        sku = card["items"][0]["sku_id"]
        ref = {"message_id": first["message"]["id"], "plan_id": card["plan_id"], "sku_id": sku}
        second = await service.handle(request("req_second", first["conversation_id"], 1, reference=ref))
        third = await service.handle(request("req_third", first["conversation_id"], 2, reference=ref))
        self.assertTrue(all(i["sku_id"] == sku for c in second["message"]["cards"] for i in c["items"]))
        self.assertTrue(any(i["sku_id"] != sku for c in third["message"]["cards"] for i in c["items"]))

    async def test_budget_followup_preserves_packaging(self):
        b = budget(6000)
        b.pop("source_turn_id")
        service = self.service(ScriptedModel(normal_patch(), patch([update(budget=b)])))
        first = await service.handle(request())
        second = await service.handle(request("req_second", first["conversation_id"], 1))
        state = self.store.session(second["conversation_id"], "local_demo")[0]["module_states"]["product_recommendation"]
        self.assertEqual(state["need_groups"][0]["budget"]["max_fen"], 6000)
        self.assertEqual(state["need_groups"][0]["constraints"][0]["field"], "package")

    async def test_user_cannot_add_identity_or_system_fields_to_request(self):
        service = self.service()
        for key in ("user_id", "member_level", "system_prompt"):
            result = await service.handle({**request(), key: "vip"})
            self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertFalse(self.model.calls)

    async def test_unsupported_is_honest_and_keeps_existing_module_state(self):
        service = self.service(ScriptedModel(normal_patch(), patch(intent="unsupported"), patch()))
        first = await service.handle(request())
        result = await service.handle(request("req_second", first["conversation_id"], 1))
        self.assertEqual(result["status"], "unsupported")
        self.assertIsNone(result["module"])
        self.assertFalse(result["message"]["cards"])
        third = await service.handle(request("req_third", first["conversation_id"], 2))
        self.assertEqual(third["status"], "completed")

    async def test_concurrent_new_identical_requests_only_commit_once(self):
        service = self.service(GateModel())
        a, b = await asyncio.gather(service.handle(request()), service.handle(request()))
        self.assertEqual(a, b)
        self.assertEqual(len(self.store.session(a["conversation_id"], "local_demo")[1]), 2)

    async def test_concurrent_existing_requests_compare_and_swap(self):
        service = self.service()
        first = await service.handle(request())
        service.module.model = GateModel()
        a, b = await asyncio.gather(service.handle(request("req_a", first["conversation_id"], 1)),
            service.handle(request("req_b", first["conversation_id"], 1)))
        self.assertEqual({a["status"], b["status"]}, {"completed", "error"})
        failed = next(x for x in [a, b] if x["status"] == "error")
        self.assertEqual(failed["error"]["code"], "revision_conflict")
        self.assertEqual(failed["revision"], 2)
        self.assertEqual(len(self.store.session(first["conversation_id"], "local_demo")[1]), 4)

    async def test_cancellation_before_model_return_does_not_commit(self):
        class WaitingModel:
            async def extract(self, payload, repair=None):
                await asyncio.Event().wait()
        service = self.service(WaitingModel())
        task = asyncio.create_task(service.handle(request()))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.store.cached("local_demo", "new", "req_test"))

    async def test_tampered_price_cannot_be_committed(self):
        service = self.service()
        original = self.module.card
        def tampered(*args, **kwargs):
            card = original(*args, **kwargs)
            card["items"][0]["unit_price_fen"] = 1
            return card
        self.module.card = tampered
        result = await service.handle(request())
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertIsNone(result["conversation_id"])

    async def test_malformed_request_is_public_error_not_exception(self):
        service = self.service()
        for value in [None, [], {"message": ""}, request(message=" ")]:
            result = await service.handle(value)
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["message"]["cards"])
