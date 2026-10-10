import json


async def main(args):
    p = args.params
    try:
        params = json.loads(p.get('params_json') or '{}')
        query = json.loads(p.get('query_result_json') or '{}')
    except (ValueError, TypeError):
        return {'params_json': '{}'}
    selected = str(p.get('order_id') or '').strip()
    if selected.lower() in ['', 'null', 'none']:
        selected = str(p.get('USER_RESPONSE') or '').strip()
    # 选择只用于补充订单号，下一次查询仍按原身份、商品和手机号重新筛选。
    params['order_id'] = selected
    params['selection_attempt'] = True
    return {'params_json': json.dumps(params, ensure_ascii=False)}
