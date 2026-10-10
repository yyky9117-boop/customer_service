"""映射主体演示身份；未绑定或未启用时禁止访问旧订单账号。"""
import re

from runtime.errors import ServiceFailure

DEMO_BINDINGS = {"U_NEW": "demo_user_a", "U_RETURN": "demo_user_b"}


def initialize_bindings(db):
    db.execute("""CREATE TABLE IF NOT EXISTS module_identity_bindings(
        module_id TEXT NOT NULL CHECK(module_id='order_query'),
        principal_id TEXT NOT NULL, external_user_id TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
        PRIMARY KEY(module_id,principal_id), UNIQUE(module_id,external_user_id))""")


class IdentityBridge:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            initialize_bindings(db)

    def bind(self, principal_id, external_user_id, *, enabled=False):
        if principal_id == "U_GUEST" or any(not isinstance(x, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{1,80}", x) is None for x in (principal_id, external_user_id)):
            raise ValueError("访客不能绑定订单，身份编号必须有效")
        with self.store.connect() as db:
            row = db.execute("SELECT external_user_id FROM module_identity_bindings WHERE module_id='order_query' AND principal_id=?",
                (principal_id,)).fetchone()
            if row and row[0] != external_user_id:
                raise ValueError("已存在身份绑定，必须显式迁移后才能更换")
            db.execute("INSERT INTO module_identity_bindings VALUES('order_query',?,?,?) "
                "ON CONFLICT(module_id,principal_id) DO UPDATE SET enabled=excluded.enabled",
                (principal_id, external_user_id, int(enabled)))

    def resolve(self, trusted_context):
        principal = trusted_context.get("user_id")
        if not principal or principal == "U_GUEST":
            raise ServiceFailure("invalid_request", "请先选择有订单绑定的演示身份，再查询订单。")
        with self.store.connect() as db:
            row = db.execute("SELECT external_user_id FROM module_identity_bindings "
                "WHERE module_id='order_query' AND principal_id=? AND enabled=1", (principal,)).fetchone()
        if row is None:
            raise ServiceFailure("invalid_request", "当前演示身份尚未启用订单绑定，不能查询其他账号的订单。")
        return row[0]
