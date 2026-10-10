"""演示数据与导出快照。不会连接或修改线上 Coze。"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'snack_orders_demo.sqlite'


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def initialize(path=DB_PATH, seed=True):
    con = connect(path)
    con.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
    # 本地初版数据库原地升级，保留已存在的订单与会话。
    if 'context_json' not in [row['name'] for row in con.execute('PRAGMA table_info(human_requests)')]:
        con.execute("ALTER TABLE human_requests ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
    if 'carrier_code' not in [row['name'] for row in con.execute('PRAGMA table_info(shipments)')]:
        con.execute("ALTER TABLE shipments ADD COLUMN carrier_code TEXT NOT NULL DEFAULT 'mock_express'")
    human_columns={row['name'] for row in con.execute('PRAGMA table_info(human_requests)')}
    for name,definition in [('transfer_state',"TEXT NOT NULL DEFAULT 'not_connected'"),('queue_position','INTEGER'),('agent_name','TEXT'),('updated_at','TEXT')]:
        if name not in human_columns:con.execute('ALTER TABLE human_requests ADD COLUMN '+name+' '+definition)
    con.execute('PRAGMA user_version=5')
    con.commit()
    if not seed:
        con.close()
        return
    with con:
        con.executemany('INSERT OR IGNORE INTO users VALUES (?,?,?,1)', [
            ('demo_user_a', '演示用户A', '13800000001'),
            ('demo_user_b', '演示用户B', '13800000002')])
        specs = [
            ('SN-DEMO-0001', 'demo_user_a', 'paid', 'unshipped', '原味薯片', None, None, None),
            ('SN-DEMO-0002', 'demo_user_a', 'paid', 'shipped', '五香牛肉干', 'DEMO-TRACK-1002', 'in_transit', '演示：包裹运输中'),
            ('SN-DEMO-0003', 'demo_user_a', 'completed', 'shipped', '每日坚果礼盒', 'DEMO-TRACK-1003', 'delivered', '演示：包裹已签收'),
            ('SN-DEMO-0004', 'demo_user_a', 'paid', 'shipped', '香辣辣条', 'DEMO-TRACK-1004', 'no_records', ''),
            ('SN-DEMO-0005', 'demo_user_a', 'paid', 'shipped', '黄油曲奇饼干', 'DEMO-TRACK-1005', 'exception', '演示：配送异常，需核实地址'),
            ('SN-DEMO-0006', 'demo_user_a', 'cancelled', 'unshipped', '海盐薯片', None, None, None),
            ('SN-DEMO-0007', 'demo_user_a', 'paid', 'unknown', '原味腰果', None, None, None),
            ('SN-DEMO-0008', 'demo_user_a', 'paid', 'partially_shipped', '零食组合装', 'DEMO-TRACK-1008A', 'in_transit', '演示：第一个包裹运输中'),
            ('SN-DEMO-0009', 'demo_user_a', 'pending_payment', 'unshipped', '麻辣牛肉粒', None, None, None),
            ('SN-DEMO-0010', 'demo_user_b', 'paid', 'shipped', '五香牛肉干', 'DEMO-TRACK-1010', 'in_transit', '演示：包裹运输中')]
        for idx, spec in enumerate(specs, 1):
            order_id, user_id, order_status, shipping_status, title, tracking, logistics, desc = spec
            phone = '13800000001' if user_id == 'demo_user_a' else '13800000002'
            con.execute('INSERT OR IGNORE INTO orders (order_id,user_id,user_phonenumber,order_status,shipping_status,amount_fen,created_at,source,legacy_tracking_number) VALUES (?,?,?,?,?,?,?,?,?)',
                        (order_id, user_id, phone, order_status, shipping_status, 2990, f'2026-10-01T09:{idx:02d}:00+08:00', 'demo', tracking))
            con.execute('INSERT OR IGNORE INTO order_items VALUES (?,?,?,?,?)', (f'ITEM-{idx}', order_id, f'SKU-{idx}', title, 1))
            if tracking:
                shipment_id = f'SHIP-{idx}'
                eta = '2026-10-08 18:00' if idx == 2 else None
                con.execute('INSERT OR IGNORE INTO shipments (shipment_id,order_id,tracking_number,carrier_name,logistics_status,latest_status_desc,estimated_delivery_at,eta_source,data_source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (shipment_id, order_id, tracking, '演示快递', logistics, desc, eta, 'demo_logistics' if eta else None, 'demo', '2026-10-07T10:00:00+08:00'))
                if logistics != 'no_records':
                    con.execute('INSERT OR IGNORE INTO tracking_events VALUES (?,?,?,?,?)', (f'EVENT-{idx}', shipment_id, '2026-10-07T10:00:00+08:00', desc, '演示地点'))
        con.execute('INSERT OR IGNORE INTO shipments (shipment_id,order_id,tracking_number,carrier_name,logistics_status,latest_status_desc,estimated_delivery_at,eta_source,data_source,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('SHIP-8B','SN-DEMO-0008','DEMO-TRACK-1008B','演示快递','no_records','',None,None,'demo','2026-10-07T10:00:00+08:00'))
    con.close()


def snapshot(path=DB_PATH, user_id=None, include_logistics=True):
    con = connect(path)
    data = []
    sql = 'SELECT * FROM orders' + (' WHERE user_id=?' if user_id is not None else '') + ' ORDER BY order_id'
    for row in con.execute(sql, (user_id,) if user_id is not None else ()):
        order = dict(row)
        order['items'] = [dict(r) for r in con.execute('SELECT sku_id,item_title,quantity FROM order_items WHERE order_id=? ORDER BY item_id', (order['order_id'],))]
        order['shipments'] = []
        for shipment_row in con.execute('SELECT * FROM shipments WHERE order_id=? ORDER BY shipment_id', (order['order_id'],)):
            shipment = dict(shipment_row)
            if include_logistics:
                shipment['tracking_events'] = [dict(r) for r in con.execute('SELECT status_time,status_desc,location FROM tracking_events WHERE shipment_id=? ORDER BY status_time DESC', (shipment['shipment_id'],))]
            else:
                shipment.update(logistics_status='unknown',latest_status_desc='',estimated_delivery_at=None,
                                eta_source=None,tracking_events=[],data_source='not_queried',updated_at=None)
            order['shipments'].append(shipment)
        data.append(order)
    con.close()
    return data


def projection(data):
    fields = ['order_id','user_id','user_phonenumber','order_status','shipping_status','amount_fen','created_at','paid_at','shipped_at','source','legacy_tracking_number','needs_review']
    return [{**{key: o.get(key) for key in fields}, 'items_json': json.dumps(o['items'], ensure_ascii=False), 'shipments_json': json.dumps(o['shipments'], ensure_ascii=False)} for o in data]


if __name__ == '__main__':
    initialize()
    data = snapshot()
    (ROOT / 'demo_orders.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / 'order_records.json').write_text(json.dumps(projection(data), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'已准备 {len(data)} 笔演示订单；全部为虚构数据。')
