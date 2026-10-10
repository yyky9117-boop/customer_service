"""旧三字段订单表迁移。order_id 已由用户确认是运单号。

输入为 JSON 记录数组；原文件不改动。无身份映射的记录隔离存放，发货状态不猜测。
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from .demo_db import connect, initialize
except ImportError:
    from demo_db import connect, initialize


def migrate(records, db_path, identity_map=None, order_map=None, shipping_map=None):
    identity_map = identity_map or {}
    order_map = order_map or {}
    shipping_map = shipping_map or {}
    initialize(db_path, seed=False)
    con = connect(db_path)
    count = 0
    with con:
        con.execute('INSERT OR IGNORE INTO users VALUES (?,?,?,1)', ('legacy_unassigned', '待确认身份的旧数据', None))
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('旧记录必须是对象')
            raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            tracking = record.get('order_id')
            tracking = None if tracking is None or str(tracking).strip().lower() in ['', 'null', 'none'] else str(tracking).strip()
            phone = str(record.get('user_phonenumber') or '').strip()
            owner = identity_map.get(phone, 'legacy_unassigned')
            con.execute('INSERT OR IGNORE INTO users VALUES (?,?,?,1)', (owner, '旧数据映射用户', phone))
            # 无商城编号时只生成明确标记的迁移编号，不冒充历史商城订单号。
            order_id = order_map.get(digest, 'LEGACY-DEMO-' + digest[:16])
            shipping = shipping_map.get(digest, 'unknown')
            if shipping not in ['unshipped','partially_shipped','shipped','unknown']:
                raise ValueError('非法发货状态映射')
            review = int(owner == 'legacy_unassigned' or shipping == 'unknown' or digest not in order_map)
            con.execute('INSERT OR IGNORE INTO orders (order_id,user_id,user_phonenumber,order_status,shipping_status,source,legacy_tracking_number,needs_review) VALUES (?,?,?,?,?,?,?,?)',
                        (order_id, owner, phone, 'unknown', shipping, 'legacy_demo', tracking, review))
            existing = con.execute('SELECT user_id,legacy_tracking_number FROM orders WHERE order_id=?', (order_id,)).fetchone()
            if existing['user_id'] != owner or existing['legacy_tracking_number'] != tracking:
                raise ValueError('商城编号映射冲突；迁移已回滚，请核对映射')
            con.execute('INSERT OR IGNORE INTO order_items VALUES (?,?,?,?,?)', ('LEGACY-ITEM-' + digest, order_id, None, str(record.get('item_title') or '商品名称待确认'), 1))
            if tracking:
                con.execute('INSERT OR IGNORE INTO shipments (shipment_id,order_id,tracking_number,logistics_status,data_source) VALUES (?,?,?,?,?)',
                            ('LEGACY-SHIP-' + digest, order_id, tracking, 'unknown', 'legacy_demo'))
            cursor = con.execute('INSERT OR IGNORE INTO legacy_order_import VALUES (?,?,?,?,?)',
                                 (digest, raw, order_id, datetime.now(timezone.utc).isoformat(), review))
            count += cursor.rowcount
    con.close()
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--identity-map')
    parser.add_argument('--order-map')
    parser.add_argument('--shipping-map')
    args = parser.parse_args()
    maps = {}
    for kind in ['identity_map','order_map','shipping_map']:
        filename = getattr(args, kind)
        maps[kind] = json.loads(Path(filename).read_text(encoding='utf-8-sig')) if filename else {}
    records = json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
    if not isinstance(records, list):
        raise ValueError('输入应为 JSON 记录数组')
    added = migrate(records, Path(args.database), **maps)
    print(f'迁移新增 {added} 条；原输入文件未修改。')
