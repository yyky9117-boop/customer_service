import json


async def main(args):
    p = args.params
    try:
        params = json.loads(p.get('params_json') or '{}')
        data = json.loads(p.get('data_json') or '[]')
    except (ValueError, TypeError):
        return {'query_result_json': json.dumps({'result_type': 'query_failed', 'reason': 'invalid_data', 'orders': []}, ensure_ascii=False), 'needs_selection': False, 'selection_question': ''}
    if not isinstance(params, dict) or not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
        return {'query_result_json': json.dumps({'result_type': 'query_failed', 'reason': 'invalid_data', 'orders': []}, ensure_ascii=False), 'needs_selection': False, 'selection_question': ''}
    failure_mode = str(p.get('failure_mode') or '')
    result = {'result_type': 'success', 'reason': '', 'query_type': params.get('query_type', 'general'),
              'orders': [], 'candidates': [], 'original_question': params.get('original_question', ''),
              'follow_up_question': params.get('follow_up_question', ''), 'is_demo': True}
    if p.get('source_is_success') is False:
        result.update(result_type='query_failed', reason='database_error')
    elif failure_mode in ['database_timeout', 'database_error']:
        result.update(result_type='query_failed', reason=failure_mode)
    elif not params.get('user_id'):
        result.update(result_type='auth_required', reason='missing_user_id')
    elif params.get('validation_error'):
        result.update(result_type='need_more_info', reason=params['validation_error'])
    elif not any(params.get(k) for k in ['order_id', 'tracking_number', 'phone_number', 'product_name']):
        result.update(result_type='need_more_info', reason='missing_identifier')
    else:
        candidates = []
        invalid_snapshot = False
        aliases = {'牛肉干': ['牛肉干', '牛肉粒'], '薯片': ['薯片'], '坚果': ['坚果', '核桃', '腰果'], '饼干': ['饼干', '曲奇'], '辣条': ['辣条']}
        for source in data:
            if str(source.get('user_id') or '') != params['user_id']:
                continue
            order = dict(source)
            try:
                for key in ['items', 'shipments']:
                    if key not in order:
                        order[key] = json.loads(order.get(key + '_json') or '[]')
                    if not isinstance(order[key], list) or any(not isinstance(x, dict) for x in order[key]):
                        raise ValueError('invalid snapshot')
            except (ValueError, TypeError):
                invalid_snapshot = True
                continue
            if params.get('order_id') and str(order.get('order_id')) != params['order_id']:
                continue
            if params.get('phone_number') and str(order.get('user_phonenumber')) != params['phone_number']:
                continue
            if params.get('tracking_number') and not any(str(s.get('tracking_number')) == params['tracking_number'] for s in order['shipments']):
                continue
            product = params.get('product_name') or ''
            if product:
                terms = aliases.get(product, [product])
                if not any(any(term.casefold() in str(i.get('item_title', '')).casefold() for term in terms) for i in order['items']):
                    continue
            # 这里只保留当前身份可访问的订单；身份、手机号不进入页面结果。
            order.pop('user_id', None)
            order.pop('user_phonenumber', None)
            order.pop('items_json', None)
            order.pop('shipments_json', None)
            candidates.append(order)
        candidates.sort(key=lambda o: (str(o.get('created_at') or ''), str(o.get('order_id') or '')), reverse=True)
        truncated = p.get('source_may_be_truncated') is True
        if invalid_snapshot:
            result.update(result_type='query_failed', reason='invalid_data')
        elif truncated:
            result.update(result_type='narrow_search', reason='source_limit_reached')
        elif not candidates:
            result.update(result_type='not_found', reason='no_matching_order')
        elif len(candidates) > 1:
            result.update(result_type='multiple_orders', candidates=candidates[:5], total_count=len(candidates))
        else:
            result['orders'] = candidates
    result['failure_mode'] = failure_mode
    question_lines = ['找到多笔匹配订单，请回复一个商城订单号：']
    for order in result.get('candidates', []):
        titles = '、'.join(str(i.get('item_title', '')) for i in order.get('items', []))
        question_lines.append(str(order['order_id']) + '：' + titles)
    if result.get('total_count', 0) > 5:
        question_lines.append('仅展示最近5笔，也可输入完整订单号。')
    return {'query_result_json': json.dumps(result, ensure_ascii=False),
            'needs_selection': result['result_type'] == 'multiple_orders',
            'selection_question': '\n'.join(question_lines)}
