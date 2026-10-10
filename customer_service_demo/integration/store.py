"""沿用主体存储布局；独立验证库避免把扩展状态写入仍在优化的网页库。"""
import json
import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import datetime

from runtime.errors import ServiceFailure
from runtime.service import SessionStore

from .contracts import validate_transition
from .identity import initialize_bindings


class UnifiedSessionStore(SessionStore):
    def __init__(self, path):
        # 首次切换前备份旧网页库；不重写原会话、历史或请求缓存。
        path = Path(path).resolve()
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "sessions" in tables and "integration_session_identities" not in tables:
                    folder = path.parent / "unified-backups"
                    folder.mkdir(parents=True, exist_ok=True)
                    backup = folder / (path.stem + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
                    with closing(sqlite3.connect(backup)) as dest:
                        source.backup(dest)
        super().__init__(path)
        with self.connect() as db:
            initialize_bindings(db)
            db.execute("CREATE TABLE IF NOT EXISTS integration_session_identities(conversation_id TEXT PRIMARY KEY REFERENCES sessions(id), principal_id TEXT NOT NULL)")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "web_contexts" in tables:
                db.execute("INSERT OR IGNORE INTO integration_session_identities SELECT s.id,c.profile FROM sessions s "
                           "JOIN web_contexts c ON c.id=s.id AND c.owner=s.owner")

    def check_principal(self, cid, principal):
        if cid is None:
            return
        with self.connect() as db:
            row = db.execute("SELECT principal_id FROM integration_session_identities WHERE conversation_id=?", (cid,)).fetchone()
        if row is None or row[0] != principal:
            raise ServiceFailure("invalid_request", "会话身份不匹配，请切换身份后开启新对话。")

    def commit(self, owner, scope, request, fingerprint, previous, state, history, response, debug, *, principal):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = db.execute("SELECT fingerprint,response FROM requests WHERE owner=? AND scope=? AND request_id=?",
                (owner, scope, request["request_id"])).fetchone()
            if cached:
                if cached[0] != fingerprint:
                    raise ServiceFailure("request_id_conflict", "相同请求编号对应不同内容，请使用新的编号。")
                return json.loads(cached[1])
            if previous:
                row = db.execute("SELECT state FROM sessions WHERE id=? AND owner=?", (previous["conversation_id"], owner)).fetchone()
                if row is None or json.loads(row[0])["revision"] != previous["revision"]:
                    raise ServiceFailure("revision_conflict", "会话已有新回复，请同步后重试。")
            validate_transition(previous, request, response, state)
            encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,history=excluded.history",
                (state["conversation_id"], owner, encoded(state), encoded(history)))
            db.execute("INSERT OR IGNORE INTO integration_session_identities VALUES(?,?)", (state["conversation_id"], principal))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)", (owner, scope, request["request_id"], fingerprint, encoded(response), encoded(debug)))
            return response
