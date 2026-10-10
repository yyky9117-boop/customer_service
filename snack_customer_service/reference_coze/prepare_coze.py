"""准备 Coze 新演示订单表的真实 API 请求体，不执行网络写入。"""
import json
from pathlib import Path

from demo_db import initialize, snapshot, projection

ROOT = Path(__file__).resolve().parent
FIELDS = {
    'order_id': ('string','商城订单号，不是快递运单号',True),
    'user_id': ('string','演示账号归属；查询时必须限定',True),
    'user_phonenumber': ('string','下单手机号，只作为查询条件',False),
    'order_status': ('string','pending_payment/paid/completed/cancelled/unknown',True),
    'shipping_status': ('string','unshipped/partially_shipped/shipped/unknown',True),
    'amount_fen': ('integer','金额，单位为分',False),
    'created_at': ('string','带时区的下单时间',False),
    'paid_at': ('string','付款时间，无数据则留空',False),
    'shipped_at': ('string','发货时间，无数据则留空',False),
    'source': ('string','demo/legacy_demo，全部为演示数据',True),
    'legacy_tracking_number': ('string','原表 order_id 的运单值，迁移保留',False),
    'needs_review': ('integer','0已确认，1需核对',True),
    'items_json': ('string','商品明细 JSON 数组',True),
    'shipments_json': ('string','包裹及物流轨迹 JSON 数组，支持多包裹',True)
}


def prepare():
    initialize()
    rows=projection(snapshot())
    schema={'workspace_id':'','table_name':'snack_orders_v2_demo','table_desc':'零食订单查询优化版；仅模拟订单；旧表不删除',
            'fields':[{'name':name,'type':kind,'desc':desc,'is_required':required} for name,(kind,desc,required) in FIELDS.items()],
            'rw_mode':'unlimited_read_write','indexs':[{'index_name':'idx_order_id','field_names':['order_id']},{'index_name':'idx_user_id','field_names':['user_id']},{'index_name':'idx_phone','field_names':['user_phonenumber']}],
            'connector_read_mode':'shared_all_connectors',
            'connector_rw_config':[{'connector_id':'1024','connector_rw_mode':'draft'}]}
    # 官方插入接口要求所有非空字段值为字符串；不把 None 写成字符串 null。
    insert={'connector_id':'1024','is_async':False,'insert_rows':[{k:str(v) for k,v in row.items() if v is not None} for row in rows]}
    for filename,value in [('coze_create_table.template.json',schema),('coze_insert_demo_records.json',insert),('coze_bindings.example.json',{'database_id':'','field_ids':{name:'' for name in FIELDS}})]:
        (ROOT/filename).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return schema,insert


if __name__=='__main__':
    schema,insert=prepare()
    print(f'已准备 {len(schema["fields"])} 个字段与 {len(insert["insert_rows"])} 笔演示订单的 Coze 请求文件。')
