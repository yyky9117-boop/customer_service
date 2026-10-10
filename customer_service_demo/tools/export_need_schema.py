"""导出模型补丁协议；runtime.needs 是唯一源定义。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.needs import PATCH_SCHEMA

schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:snack-customer-service:need-patch:v1", **PATCH_SCHEMA}
(ROOT / "schemas/need-patch.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("need patch schema exported")
