"""把验收资产整理成人可读索引；保留未执行标记。"""

from collections import Counter
from build_stage2_data import ROOT
import json


def read(name, folder="acceptance"):
    return json.loads((ROOT / folder / name).read_text(encoding="utf-8"))


def text(value):
    return str(value).replace("|", "／").replace("\n", " ").replace("\t", " ")


def money(value):
    return "未知" if value is None else f"{value // 100}.{value % 100:02d}"


def build():
    cases = read("cases.json")
    goldens = read("pricing_goldens.json")
    labels = {"language": "语言理解", "multi_turn": "多轮与多人", "filtering": "商品准入",
              "retrieval": "检索", "pricing": "优惠与金额", "ranking": "排序与凑单",
              "facts": "事实与表达", "adversarial": "对抗内容", "reliability": "故障、状态与界面"}
    lines = ["# 验收案例索引", "", f"已定义 {len(cases)} 个验收场景与 {len(goldens)} 个指定购物方案数值基准。",
             "", ("**这是原始验收标准索引，执行结果见 [第五步验收记录](09-第五步业务验收.md)。执行过不等于通过，数值基准通过也不代表对话能力通过。**" if any(c['executed'] for c in cases) else "**验收场景均未运行。数值基准通过独立参考检查，仅证明固定资料与手算期望一致，不代表推荐服务或模型通过。**"),
             "", "所有数据虚构；固定时间 2026-10-07 12:00:00（Asia/Shanghai）。P0 项必须逐项通过，不能用平均评分抵消。关键 AI 场景后续重复至少 5 次。", ""]
    counts = Counter(c["category"] for c in cases)
    lines += ["| 类别 | 场景数 |", "|---|---:|"]
    lines += [f"| {labels[key]} | {count} |" for key, count in counts.items()]
    for category, label in labels.items():
        lines += ["", "## " + label, "", "| ID | 场景 / 输入 | 核对要点 |", "|---|---|---|"]
        for case in cases:
            if case["category"] != category:
                continue
            turns = " → ".join(text(t) for t in case["turns"])
            checks = "；".join(text(c) for c in case["expected"]["checks"])
            lines.append(f"| {case['id']} | **{text(case['title'])}**：{turns} | {checks} |")
    lines += ["", "## 指定购物方案数值基准", "", "这些基准固定商品与优惠组合，不声称枚举了最优方案。拒绝指定优惠组合不等于整个用户请求失败：推荐服务应尝试其他合法组合。", "",
              "| ID | 边界 | 活动 / 券 | 商品应付（元） | 运费（元） | 总价（元）或拒绝原因 |", "|---|---|---|---:|---:|---|"]
    for case in goldens:
        expected = case["expected"]
        selected = (case["activity_id"] or "无") + " / " + (case["coupon_id"] or "无")
        if expected["eligible"]:
            lines.append(f"| {case['id']} | {text(case['title'])} | {selected} | {money(expected['merchandise_payable_fen'])} | {money(expected['shipping_fen'])} | {money(expected['total_payable_fen'])} |")
        else:
            lines.append(f"| {case['id']} | {text(case['title'])} | {selected} | — | — | {expected['rejection_code']} |")
    lines += ["", "数据检查：`python tools/check_stage2_assets.py`。协议与资产检查：`python -m unittest discover -s tests -v`。", ""]
    (ROOT / "docs" / "03-验收案例索引.md").write_text("\n".join(lines), encoding="utf-8")
    products = read("products.json", "data")
    data_lines = ["# 演示数据目录", "", "全部商品、用户、券和活动均为虚构测试资料。价格、标签和卖点仅用于验证本 Demo。", "",
                  "默认身份为匿名 U_GUEST，不自动认定新客、会员或持券。缺失字段使用 null 或明确 unknown；空列表表示资料明确无该项。", "",
                  "| SKU | 商品 | 标价（元） | 辣度 | 糖标签 | 内包装 | 库存 | 边界用途 |", "|---|---|---:|---|---|---|---:|---|"]
    spice_labels = {"none": "不辣", "mild": "微辣", "hot": "重辣", "unknown": "未知"}
    sugar_labels = {"regular": "普通", "low_sugar": "低糖", "sugar_free": "无糖", "unknown": "未知"}
    special = {"P006": "可能含花生", "P008": "甜度低但不是低糖标签", "P009": "无糖但有甜味", "P014": "礼盒内容含坚果", "P016": "可能含花生", "P022": "缺货", "P023": "资料未知", "P024": "临期", "P025": "过期", "P026": "价格未知", "P029": "独立包装但单条 35g"}
    for p in products:
        pack = {True: "独立", False: "非独立", None: "未知"}[p["packaging"]["individually_wrapped"]]
        data_lines.append(f"| {p['sku_id']} | {p['product_name']} | {money(p['price_fen'])} | {spice_labels[p['spice_level']]} | {sugar_labels[p['sugar_label']]} | {pack} | {p['stock_quantity']} | {special.get(p['sku_id'], '普通候选')} |")
    data_lines += ["", "## 演示用户", "", "| 用户 | 会员 | 新 / 老客 | 配送区域 | 券状态用途 |", "|---|---|---|---|---|"]
    for u in read("users.json", "data"):
        desc = "未知" if u["owned_coupons"] is None else ("没有持券" if not u["owned_coupons"] else ", ".join(sorted({h["state"] for h in u["owned_coupons"]})))
        data_lines.append(f"| {u['user_id']} | {u['member_level'] or '未知'} | {u['customer_type'] or '未知'} | {u['shipping_zone']} | {desc} |")
    for file, title, key in (("activities.json", "演示活动", "activity_id"), ("coupons.json", "演示券", "coupon_id")):
        data_lines += ["", "## " + title, "", "| ID | 名称 | 适用品类 / SKU | 叠加说明 |", "|---|---|---|---|"]
        for row in read(file, "data"):
            applies = ", ".join(row["scope"]["categories"] + row["scope"]["sku_ids"]) or "全商品"
            stack_key = "stack_with_coupon" if key == "activity_id" else "stack_with_activity"
            stacking = "允许（须同时满足另一方）" if row[stack_key] else "不允许"
            data_lines.append(f"| {row[key]} | {row['name']} | {applies} | {stacking} |")
    data_lines += ["", "完整有效期、门槛、资格、库存与规则以 data 下的 JSON 为准；表格是阅读摘要。", ""]
    (ROOT / "docs" / "04-演示数据目录.md").write_text("\n".join(data_lines), encoding="utf-8")


if __name__ == "__main__":
    build()
