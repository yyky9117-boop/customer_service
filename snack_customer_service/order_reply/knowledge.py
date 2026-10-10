"""订单场景过滤与关键词评分检索，数据和调试归订单模块管理。"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def seed_knowledge(db, replace=False):
    bank = json.loads((ROOT / 'knowledge.json').read_text(encoding='utf-8'))
    if not isinstance(bank.get('version'),str) or not bank['version'] or not isinstance(bank.get('source'),str) or not bank['source']:
        raise ValueError('话术库必须标明版本和来源')
    records=[]
    for e in bank['entries']:
        if (any(not isinstance(e.get(key),str) or not e[key] or len(e[key])>2000 for key in ('id','scene','guide','opening','closing')) or
                not isinstance(e.get('keywords'),list) or not all(isinstance(w,str) and 0<len(w)<=80 for w in e['keywords'])):
            raise ValueError('话术条目字段不完整或长度不合法')
        records.append((e['id'],e['scene'],json.dumps(e['keywords'],ensure_ascii=False),e['guide'],e['opening'],e['closing'],bank['source'],bank['version']))
    if len({r[0] for r in records})!=len(records):raise ValueError('话术条目编号不能重复')
    sql='INSERT OR IGNORE INTO order_reply_knowledge VALUES(?,?,?,?,?,?,?,?,1)'
    if replace:
        sql='INSERT INTO order_reply_knowledge VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(entry_id) DO UPDATE SET '+', '.join(
            name+'=excluded.'+name for name in ('scene','keywords_json','guide','opening','closing','source','version','enabled'))
    db.executemany(sql,records)


class KnowledgeRetriever:
    def __init__(self, db_path):
        self.db_path = Path(db_path).resolve()

    def retrieve(self, scene, message, limit=2):
        with closing(sqlite3.connect(self.db_path.as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM order_reply_knowledge WHERE scene=? AND enabled=1', (scene,))]
        for row in rows:
            words = json.loads(row.pop('keywords_json'))
            matches = [w for w in words if w in message]
            row.update(score=10+sum(len(w)*(3 if w in ('急','着急','等了','催','赶紧') else 1) for w in matches), matched_keywords=matches)
        rows.sort(key=lambda r:(-r['score'], r['entry_id']))
        return rows[:limit]


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='订单模块独立话术库管理；显式同步不会改动订单和会话')
    parser.add_argument('--sync',action='store_true',help='把 knowledge.json 同编号条目写入数据库，覆盖这些条目的旧内容')
    parser.add_argument('--database',default=str(ROOT.parent/'database/snack_orders_demo.sqlite'))
    args=parser.parse_args()
    path=Path(args.database).resolve()
    if not args.sync:parser.error('需要显式指定 --sync；正常启动不会覆盖已有话术编辑')
    if not path.is_file() or not path.is_relative_to(ROOT.parent):parser.error('只能同步本项目内已存在的数据库')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)) as db, db:
        seed_knowledge(db,replace=True)
    print('订单模块话术库已同步；订单、物流、会话与工单数据未改写。')
