import json
import re


async def main(args):
    p = args.params
    raw = p.get('extracted', {key: p.get(key) for key in ['order_id', 'tracking_number', 'phone_number', 'product_name', 'query_type']})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cleaned = {}
    for key in ['order_id', 'tracking_number', 'phone_number', 'product_name']:
        value = raw.get(key)
        cleaned[key] = '' if value is None or str(value).strip().lower() in ['', 'null', 'none'] else str(value).strip()
    query_type = str(raw.get('query_type') or 'general')
    cleaned['query_type'] = query_type if query_type in ['general', 'shipping', 'logistics', 'delivery_time'] else 'general'
    original = str(p.get('USER_INPUT') or '')
    cleaned['original_question'] = original
    # 身份必须来自调用方；模型提取结果不能覆盖身份。
    cleaned['user_id'] = str(p.get('user_id') or '').strip()
    context = p.get('context_json') or '{}'
    try:
        context = json.loads(context) if isinstance(context, str) else context
    except (ValueError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    if not any(cleaned[k] for k in ['order_id', 'tracking_number', 'phone_number', 'product_name']):
        cleaned['order_id'] = str(context.get('selected_order_id') or '').strip()
    valid_phone = not cleaned['phone_number'] or bool(re.fullmatch(r'1[3-9]\d{9}', cleaned['phone_number']))
    cleaned['validation_error'] = '' if valid_phone else 'invalid_phone'
    if not valid_phone:
        cleaned['phone_number'] = ''
    has_identifier = any(cleaned[k] for k in ['order_id', 'tracking_number', 'phone_number', 'product_name'])
    ready = bool(cleaned['user_id'] and has_identifier and not cleaned['validation_error'])
    return {'params_json': json.dumps(cleaned, ensure_ascii=False), 'ready': ready,
            'question': '请提供商城订单号、快递运单号或下单手机号；也可以告诉我购买的商品名称。',
            'user_id': cleaned['user_id']}
