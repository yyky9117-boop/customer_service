import json
import re


async def main(args):
    p = args.params
    try:
        base = json.loads(p.get('params_json') or '{}')
    except (ValueError, TypeError):
        base = {}
    for key in ['order_id', 'tracking_number', 'phone_number', 'product_name']:
        value = p.get(key)
        if value is not None and str(value).strip().lower() not in ['', 'null', 'none']:
            base[key] = str(value).strip()
    # 用户追问回答补充查询条件，不覆盖服务端 user_id 与原始 query_type。
    base['follow_up_question'] = str(p.get('USER_RESPONSE') or '')
    phone = base.get('phone_number') or ''
    base['validation_error'] = '' if not phone or re.fullmatch(r'1[3-9]\d{9}', phone) else 'invalid_phone'
    if base['validation_error']:
        base['phone_number'] = ''
    return {'params_json': json.dumps(base, ensure_ascii=False)}
