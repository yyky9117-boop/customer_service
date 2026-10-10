import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from runtime.catalog import Catalog, instant
from runtime.module_interface import ModuleContext
from runtime.needs import merge_patch
from runtime.recommendation import RecommendationModule
from runtime.validation import validate_response

example = json.loads((ROOT / "examples/need-office.json").read_text(encoding="utf-8"))
catalog = Catalog.load()
now = instant(catalog.data["manifest"]["reference_time"])
context = ModuleContext("conv_offline", "req_offline", "turn_offline", 0, example["message"], {}, (), None,
    now, {"owner_id": "local_demo", "user_id": "U_GUEST", "channel": "web"})
state = merge_patch({}, example["patch"], context)
result = RecommendationModule(catalog, None).recommend(state, context)
response = {"schema_version": "1.0", "conversation_id": "conv_offline", "request_id": "req_offline", "turn_id": "turn_offline",
    "base_revision": 0, "revision": 1, "module": "product_recommendation", "status": result.status,
    "message": {"id": "msg_offline", "role": "assistant", "content": result.content, "cards": list(result.cards)},
    "clarification": result.clarification, "error": None}
validate_response(response)
artifact = {"mode": "structured_input_offline", "live_model_used": False, "session_persisted": False,
    "reference_time": now.isoformat(), "input": example["message"], "result": response, "debug": result.debug}
(ROOT / "examples/stage3-offline-result.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(result.content)
