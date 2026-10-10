"""权威结构化演示资料；召回文本只提供 SKU 线索。"""
import copy
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .errors import ServiceFailure

ROOT = Path(__file__).resolve().parents[1]


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return result


class Catalog:
    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.quarantined = {}
        for sku, row in list(self.data["products"].items()):
            price, stock = row.get("price_fen"), row.get("stock_quantity")
            invalid = (price is not None and (type(price) is not int or price < 0)) or (
                stock is not None and (type(stock) is not int or stock < 0))
            try:
                invalid |= not isinstance(row.get("packaging"), dict)
                invalid |= any(row.get(key) is not None and (not isinstance(row[key], list)
                    or not all(isinstance(x, str) for x in row[key])) for key in
                    ("ingredients", "declared_allergens", "may_contain_allergens", "scene_tags"))
                invalid |= any(key not in row for key in ("price_fen", "stock_quantity", "expires_at", "category", "image_url"))
                if row.get("expires_at") is not None:
                    instant(row["expires_at"])
                invalid |= row.get("sku_id") != sku or not isinstance(row.get("product_name"), str) or not row.get("product_name")
            except (ValueError, TypeError):
                invalid = True
            if invalid:
                self.quarantined[sku] = self.data["products"].pop(sku)

    @classmethod
    def load(cls, folder=ROOT / "data"):
        keys = {"products": "sku_id", "users": "user_id", "activities": "activity_id", "coupons": "coupon_id"}
        try:
            data = {}
            for name, key in keys.items():
                rows = json.loads((Path(folder) / f"{name}.json").read_text(encoding="utf-8"))
                data[name] = {r[key]: r for r in rows}
                if len(data[name]) != len(rows):
                    raise ValueError("资料 ID 重复")
            data["manifest"] = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
            return cls(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ServiceFailure("data_unavailable", "商品资料暂时无法读取，请稍后重试。", True) from exc

    @property
    def products(self):
        return self.data["products"]

    def user(self, user_id):
        # 未知可信身份也不能提升为新客或会员。
        return copy.deepcopy(self.data["users"].get(user_id, self.data["users"]["U_GUEST"]))

    def recall(self, hints=()):
        seen = set()
        # 有限全库补召回，避免首次检索为空或漏召回造成假无匹配。
        for sku in [*hints, *sorted(self.products)]:
            if sku in self.products and sku not in seen:
                seen.add(sku)
                yield self.products[sku]

    def recommendable_expiry(self, row, now):
        """全店推荐门槛：到期日已确认，且距离本地到期日严格超过临期天数。"""
        if not row or not row.get("expires_at"):
            return False
        expiry = instant(row["expires_at"])
        local = timezone(timedelta(hours=8))
        days = (expiry.astimezone(local).date() - now.astimezone(local).date()).days
        return expiry > now and days > self.data["manifest"].get("near_expiry_days", 30)
