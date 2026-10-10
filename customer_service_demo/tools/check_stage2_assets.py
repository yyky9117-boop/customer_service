"""检查固定资料与验收期望，不能宣称模型或推荐服务通过。"""

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from acceptance.reference_probe import read, dataset, dt, check_golden

CATEGORIES = {"biscuits", "seaweed", "beans", "meat", "nuts", "chocolate", "candy", "dried_fruit", "popcorn", "grain_bar", "gift_box", "mixed", "drinks", "seeds"}
ALLERGENS = {"gluten", "soy", "tree_nuts", "peanut", "milk", "sesame", "egg", "fish", "shellfish"}
MEMBERS = {"ordinary", "vip"}
CUSTOMERS = {"new", "returning"}


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def integer(value, label, nullable=False):
    need(nullable and value is None or type(value) is int and value >= 0, label + " 应为非负整数或明确允许的空值")


def check_product(p):
    integer(p["price_fen"], p["sku_id"] + ".price", True)
    integer(p["stock_quantity"], p["sku_id"] + ".stock", True)
    expected = "unknown" if p["stock_quantity"] is None else ("in_stock" if p["stock_quantity"] else "out_of_stock")
    need(p["stock_status"] == expected, "库存数量与状态矛盾")
    need(p["category"] in CATEGORIES and p["is_demo"] is True and p["currency"] == "CNY", "商品身份/品类/币种错误")
    need(p["spice_level"] in ("none", "mild", "hot", "unknown"), "辣度错误")
    need(p["sweetness"] in ("none", "low", "medium", "high", "unknown"), "甜度错误")
    need(p["sugar_label"] in ("regular", "low_sugar", "sugar_free", "unknown"), "糖标签错误")
    for key in ("declared_allergens", "may_contain_allergens"):
        value = p[key]
        need(value is None or isinstance(value, list) and len(value) == len(set(value)) and set(value) <= ALLERGENS, "过敏原标签错误")
    need(p["ingredients"] is None or isinstance(p["ingredients"], list) and all(isinstance(i, str) and i for i in p["ingredients"]), "原料格式错误")
    pack = p["packaging"]
    need(pack["outer_type"] in ("bag", "box", "bottle"), "外包装错误")
    need(pack["individually_wrapped"] is None or type(pack["individually_wrapped"]) is bool, "内包装标识错误")
    integer(pack["inner_weight_g"], "内包装重量", True)
    if p["expires_at"] is not None:
        dt(p["expires_at"])
    dt(p["source_updated_at"])


def check_catalog(data):
    for p in data["products"].values():
        check_product(p)
    for name in ("activities", "coupons"):
        for record in data[name].values():
            need(record["is_demo"] is True, "优惠非演示数据")
            need(dt(record["start_at"]) < dt(record["end_at"]), "有效期顺序错误")
            need(set(record["scope"]["sku_ids"]) <= set(data["products"]), "优惠引用未知 SKU")
            need(set(record["scope"]["categories"]) <= CATEGORIES, "优惠引用未知品类")
            need(set(record["audience"]["member_levels"]) <= MEMBERS, "会员属性错误")
            need(set(record["audience"]["customer_types"]) <= CUSTOMERS, "新老客属性错误")
            for key in ("threshold_fen", "minus_fen", "discount_off_bps"):
                integer(record[key], key)
            need(record["discount_off_bps"] <= 10000, "折扣超过 100%")
            if name == "activities":
                need(record["type"] in ("amount_off", "percentage", "gift", "fixed_price", "unknown"), "活动类型错误")
                integer(record["minimum_quantity"], "活动数量")
                need(record["minimum_quantity"] >= 1, "活动最小数量为零")
                integer(record["maximum_quantity"], "活动最大数量", True)
                integer(record["fixed_unit_price_fen"], "固定单价", True)
                if record["gift"]:
                    need(record["gift"]["sku_id"] in data["products"], "赠品 SKU 不存在")
                    integer(record["gift"]["quantity"], "赠品数量")
                    need(record["gift"]["quantity"] > 0, "赠品数量不能为零")
                need(record["rule_complete"] or record["type"] == "unknown", "不完整活动须明确未知类型")
            else:
                need(record["type"] in ("amount_off", "percentage"), "券类型错误")
                integer(record["cap_fen"], "券封顶", True)
    for user in data["users"].values():
        need(user["member_level"] is None or user["member_level"] in MEMBERS, "用户会员级别错误")
        need(user["customer_type"] is None or user["customer_type"] in CUSTOMERS, "用户新老客属性错误")
        need(user["shipping_zone"] in data["manifest"]["shipping"]["zones"], "用户配送区域错误")
        holdings = user["owned_coupons"]
        if holdings is not None:
            need(len(holdings) == len({h["coupon_id"] for h in holdings}), "用户券重复")
            for hold in holdings:
                need(hold["coupon_id"] in data["coupons"] and hold["state"] in ("unused", "used", "locked"), "持券引用/状态错误")


def verify():
    data = dataset()
    manifest = data["manifest"]
    dt(manifest["reference_time"])
    need(manifest["is_demo"] is True and manifest["default_user_id"] == "U_GUEST", "默认身份不能模拟新客/VIP")
    for collection, id_key, manifest_key in (
        ("products", "sku_id", "product_ids"), ("users", "user_id", "user_ids"),
        ("activities", "activity_id", "activity_ids"), ("coupons", "coupon_id", "coupon_ids"),
    ):
        rows = read(collection + ".json")
        ids = [r[id_key] for r in rows]
        need(len(ids) == len(set(ids)), collection + " 主键重复")
        need(ids == manifest[manifest_key], collection + " 清单不一致")
    check_catalog(data)
    fixtures = read("fixtures.json", "acceptance")
    fixture_ids = {f["fixture_id"] for f in fixtures}
    need(len(fixture_ids) == len(fixtures), "覆盖样本 ID 重复")
    for fixture in fixtures:
        for patch in fixture["patches"]:
            need(patch["collection"] in data and patch["id"] in data[patch["collection"]], "覆盖目标不存在")
            need(set(patch["set"]) <= set(data[patch["collection"]][patch["id"]]), "覆盖字段不存在")
        try:
            check_catalog(dataset(fixture["fixture_id"]))
        except AssertionError:
            need(fixture.get("expected_data_valid") is False, "意外无效的覆盖样本 " + fixture["fixture_id"])
        else:
            need(fixture.get("expected_data_valid", True), "声明无效的样本实际上有效")
        if "duplicate_product_id" in fixture:
            need(fixture["duplicate_product_id"] in data["products"], "重复召回引用不存在")
    goldens = read("pricing_goldens.json", "acceptance")
    golden_ids = {g["id"] for g in goldens}
    need(len(golden_ids) == len(goldens), "数值基准 ID 重复")
    for golden in goldens:
        need(golden["context"]["user_id"] in data["users"], "数值基准身份未知")
        need(golden["fixture_id"] is None or golden["fixture_id"] in fixture_ids, "数值基准覆盖引用错误")
        need(golden["activity_id"] is None or golden["activity_id"] in data["activities"], "活动引用错误")
        need(golden["coupon_id"] is None or golden["coupon_id"] in data["coupons"], "券引用错误")
        need(len(golden["items"]) == len({i["sku_id"] for i in golden["items"]}), "数值输入重复商品")
        for item in golden["items"]:
            need(item["sku_id"] in data["products"] and type(item["quantity"]) is int and item["quantity"] > 0, "数值输入商品或数量错误")
        check_golden(golden)
    cases = read("cases.json", "acceptance")
    rule_ids = {r["id"] for r in read("rules.json", "acceptance")["rules"]}
    need(len(cases) == len({c["id"] for c in cases}), "验收案例 ID 重复")
    referenced_rules = set()
    referenced_goldens = set()
    for case in cases:
        need(case["context"]["user_id"] in data["users"], "验收身份未知")
        need(case["turns"] and all(isinstance(turn, str) for turn in case["turns"]), "案例缺少输入")
        need(case["rule_ids"] and set(case["rule_ids"]) <= rule_ids, "规则引用错误")
        need(case["fixture_id"] is None or case["fixture_id"] in fixture_ids, "验收覆盖引用错误")
        need(case["catalog_subset"] is None or set(case["catalog_subset"]) <= set(data["products"]), "验收商品引用错误")
        need(set(case["golden_ids"]) <= golden_ids, "数值基准引用错误")
        expected = case["expected"]
        need(expected["checks"] and expected["failure_condition"], "案例缺少具体预期/失败条件")
        need(set(expected["allowed_final_status"]) <= {"completed", "needs_clarification", "no_match", "unsupported", "error"}, "验收状态不在协议中")
        need(type(case['executed']) is bool, '执行状态必须是布尔值')
        if case['executed']:
            evidence_path=(ROOT/case.get('execution_evidence','')).resolve()
            need(evidence_path.is_relative_to(ROOT.resolve()) and evidence_path.is_file(), '执行状态必须有项目内实际证据文件')
            evidence=json.loads(evidence_path.read_text(encoding='utf-8'))
            need(evidence.get('case_id')==case['id'] and bool(evidence.get('turns')), '案例执行证据不属于对应场景')
        need(case["repeat_count"] >= 1, "重复次数错误")
        referenced_rules.update(case["rule_ids"])
        referenced_goldens.update(case["golden_ids"])
    need(referenced_rules == rule_ids, "业务规则存在未覆盖项")
    coverage = read("coverage.json", "acceptance")
    need(coverage["semantic_case_count"] == len(cases) and coverage["pricing_probe_count"] == len(goldens), "覆盖清单数量不一致")
    need(coverage['all_cases_executed']==all(c['executed'] for c in cases), '覆盖清单与逐项执行状态不一致')
    return {"products": len(data["products"]), "users": len(data["users"]), "activities": len(data["activities"]),
            "coupons": len(data["coupons"]), "fixtures": len(fixtures), "semantic_cases": len(cases),
            "pricing_probes_checked": len(goldens), "rules": len(rule_ids),
            "categories": dict(Counter(c["category"] for c in cases)),
            "service_or_model_cases_executed": sum(c['executed'] for c in cases)}


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
