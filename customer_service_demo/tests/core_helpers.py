import copy
import json
from pathlib import Path
import shutil
import uuid

from runtime.catalog import Catalog, instant
from runtime.module_interface import ModuleContext
from runtime.needs import empty_budget

ROOT = Path(__file__).resolve().parents[1]
NOW = instant("2026-10-07T12:00:00+08:00")


class WorkspaceTemp:
    """临时资料只写到已授权项目目录，清理前核对绝对路径。"""
    def __init__(self):
        self.base = (ROOT / ".local" / "tests").resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        if not self.base.is_relative_to(ROOT.resolve()):
            raise ValueError("测试目录越界")
        target = self.base / ("case_" + uuid.uuid4().hex)
        target.mkdir()
        self.name = str(target)

    def cleanup(self):
        target = Path(self.name).resolve()
        if target == self.base or not target.is_relative_to(self.base):
            raise ValueError("拒绝清理越界路径")
        shutil.rmtree(target)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()


def catalog(fixture_id=None, subset=None):
    result = Catalog.load()
    data = copy.deepcopy(result.data)
    if fixture_id:
        fixture = next(f for f in json.loads((ROOT / "acceptance/fixtures.json").read_text(encoding="utf-8")) if f["fixture_id"] == fixture_id)
        for patch in fixture["patches"]:
            data[patch["collection"]][patch["id"]].update(copy.deepcopy(patch["set"]))
    if subset is not None:
        data["products"] = {s: p for s, p in data["products"].items() if s in subset}
    return Catalog(data)


def constraint(field, value, operator="equals", strength="hard", cid=None, **extra):
    return {"constraint_id": cid or "c_" + field, "field": field, "value": value, "operator": operator,
        "strength": strength, "source_text": "测试需求", "source_turn_id": "turn_test", "origin": "explicit",
        "derived_from": [], "status": "active", **extra}


def group(constraints=(), budget=None, gid="group_self"):
    return {"group_id": gid, "label": "测试对象", "constraints": list(constraints), "budget": budget or empty_budget()}


def state(*groups, shared=None):
    return {"need_groups": list(groups), "shared_budget": shared, "pending_clarification": None, "last_presentation": None}


def budget(maximum, scope="goods_total", minimum=None, strict="strict", min_is_hard=False):
    return {"min_fen": minimum, "max_fen": maximum, "scope": scope, "strictness": strict,
        "source_text": "测试需求", "source_turn_id": "turn_test", "min_is_hard": min_is_hard}


def context(module_state=None, message="测试需求", uid="U_GUEST"):
    return ModuleContext("conv_test", "req_test", "turn_test", 0, message, module_state or {}, (), None, NOW,
        {"owner_id": "local_demo", "user_id": uid, "channel": "web"})


def patch(groups=(), **extra):
    return {"intent": "recommend", "reset": False, "groups": list(groups), "remove_group_ids": [],
        "clarification": None, "retrieval_hints": [], **extra}


def update(gid="group_self", constraints=(), remove=(), **extra):
    constraints = [{k: v for k, v in c.items() if k not in {"status", "source_turn_id"}} for c in constraints]
    return {"group_id": gid, "upsert_constraints": constraints, "remove_constraint_ids": list(remove), **extra}


class ScriptedModel:
    """只用于验证集成边界的脚本输出，不能用于宣称自然语言理解通过。"""
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    async def extract(self, payload, repair=None):
        self.calls.append({"payload": copy.deepcopy(payload), "repair": repair})
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)
