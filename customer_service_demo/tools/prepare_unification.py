"""只添加默认禁用的订单身份映射；不替换主体业务数据或会话历史。"""
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration.identity import DEMO_BINDINGS, initialize_bindings


def preserved_tables(db):
    result = {}
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ("sessions", "requests", "web_contexts"):
        if name in names:
            rows = db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
            payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
            result[name] = {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def prepare(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("准备脚本只处理本项目中已存在的会话数据库")
    backup = ROOT / ".local/unification-backups" / datetime.now().strftime("migration-%Y%m%d-%H%M%S-%f.sqlite3")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        before = preserved_tables(db)
        initialize_bindings(db)
        for principal, external in DEMO_BINDINGS.items():
            existing = db.execute("SELECT external_user_id FROM module_identity_bindings WHERE principal_id=? AND module_id='order_query'", (principal,)).fetchone()
            if existing and existing[0] != external:
                raise ValueError("现有映射不同，已停止，未覆盖现有身份关系")
            db.execute("INSERT OR IGNORE INTO module_identity_bindings VALUES('order_query',?,?,0)", (principal, external))
        after = preserved_tables(db)
        if before != after:
            raise ValueError("主体数据发生变化，准备事务已回滚")
        bindings = [dict(zip(("principal_id", "external_user_id", "enabled"), row)) for row in db.execute(
            "SELECT principal_id,external_user_id,enabled FROM module_identity_bindings ORDER BY principal_id")]
    report = {"prepared_at": datetime.now(timezone.utc).isoformat(), "database": str(path), "backup": str(backup),
        "preserved_tables": after, "preserved_data_verified": True, "bindings": bindings,
        "web_routing_enabled": False, "legacy_order_database_modified": False}
    out = ROOT / "acceptance/unification-migration.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="初步统一的数据库准备")
    parser.add_argument("--database", default=str(ROOT / ".local/web-sessions.sqlite3"))
    args = parser.parse_args()
    report = prepare(args.database)
    print(json.dumps({"preserved_data_verified": report["preserved_data_verified"], "bindings": report["bindings"],
        "web_routing_enabled": False}, ensure_ascii=False, indent=2))
