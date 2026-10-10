import json


async def main(args):
    p = args.params
    try:
        query = json.loads(p.get('query_result_json') or '{}')
        if not isinstance(query, dict):
            raise ValueError('invalid result')
    except (ValueError, TypeError):
        query = {'result_type': 'query_failed', 'reason': 'invalid_data', 'orders': []}
    result_type = query.get('result_type', 'query_failed')
    orders = query.get('orders', [])
    query_type = query.get('query_type', 'general')
    next_action = 'none'
    transfer_reason = ''
    replies = {'query_failed': '本次查询未成功，请稍后重试，或联系人工客服核实。',
               'auth_required': '请先登录商城，再查询你的订单。',
               'need_more_info': '请补充有效的商城订单号、快递运单号、下单手机号或商品名称。',
               'not_found': '没有找到与你的账号及查询条件匹配的订单，请核对后重试。',
               'narrow_search': '匹配范围较大，请提供完整订单号或更具体的商品名称。',
               'multiple_orders': '匹配到多笔订单，请选择需要查询的商城订单号。'}
    reply = replies.get(result_type, '')
    if result_type in ['need_more_info', 'narrow_search']:
        next_action = 'provide_info'
    elif result_type == 'multiple_orders':
        next_action = 'select_order'
    elif result_type == 'auth_required':
        next_action = 'login'
    elif result_type == 'query_failed':
        next_action = 'retry'
    elif result_type == 'not_found':
        next_action = 'verify_query'
    if result_type == 'success' and len(orders) == 1:
        order = orders[0]
        order_id = str(order.get('order_id', ''))
        shipping_status = order.get('shipping_status', 'unknown')
        order_status = order.get('order_status', 'unknown')
        if order_status == 'cancelled':
            reply = '订单' + order_id + '已取消。'
        elif order_status == 'pending_payment':
            reply = '订单' + order_id + '待付款。'
        elif shipping_status == 'unshipped':
            reply = '订单' + order_id + '已付款，尚未发货。' if order_status == 'paid' else '订单' + order_id + '尚未发货。'
            if query_type == 'delivery_time':
                reply += '目前暂无预计送达时间。'
        elif shipping_status == 'unknown':
            reply = '订单' + order_id + '的发货状态暂未确认，需要人工客服核实。'
            next_action = 'request_human'
            transfer_reason = 'unknown_shipping_status'
        elif shipping_status in ['shipped', 'partially_shipped']:
            partial = shipping_status == 'partially_shipped'
            reply = '订单' + order_id + ('已部分发货。' if partial else '已发货。')
            shipments = order.get('shipments') or []
            if query_type != 'shipping':
                if query.get('failure_mode') in ['logistics_timeout', 'logistics_error']:
                    result_type = 'partial_success'
                    reply += '本次物流查询未成功，请稍后重试。'
                    next_action = 'retry'
                elif not shipments:
                    result_type = 'partial_success'
                    reply += '暂未取得运单信息，需要人工核实。'
                    next_action = 'request_human'
                    transfer_reason = 'missing_shipment'
                else:
                    parts = []
                    etas = []
                    for shipment in shipments:
                        status = shipment.get('logistics_status', 'unknown')
                        description = str(shipment.get('latest_status_desc') or '')
                        if status=='query_failed':
                            result_type='partial_success'
                            parts.append('本次物流查询未成功，请稍后重试')
                            etas.append('本次物流查询未成功，暂无可靠预计送达时间')
                            next_action='retry'
                            continue
                        if query.get('failure_mode') == 'logistics_empty':
                            status, description = 'no_records', ''
                        if query_type == 'delivery_time':
                            eta = None if query.get('failure_mode') == 'logistics_empty' else shipment.get('estimated_delivery_at')
                            if status == 'delivered':
                                etas.append('包裹已签收')
                            elif eta:
                                etas.append('包裹预计' + str(eta) + '送达，以后续物流更新为准')
                            else:
                                etas.append('暂无预计送达时间')
                        elif status == 'no_records':
                            parts.append('暂未获取到物流轨迹')
                        elif description:
                            parts.append(description)
                        else:
                            parts.append('物流状态暂未确认')
                        if status == 'exception':
                            next_action = 'request_human'
                            transfer_reason = 'logistics_exception'
                        elif shipment.get('scenario')=='stalled':
                            next_action='request_human'
                            transfer_reason='tracking_stalled'
                    reply += '；'.join(etas if query_type == 'delivery_time' else parts) + '。'
        else:
            result_type = 'query_failed'
            reply = replies['query_failed']
            next_action = 'retry'
    elif result_type == 'success':
        result_type = 'query_failed'
        reply = replies['query_failed']
        next_action = 'retry'
    original = str(query.get('original_question') or '') + ' ' + str(query.get('follow_up_question') or '')
    if any(word in original for word in ['退款', '赔偿', '退货', '换货', '投诉', '差评', '没收到', '未收到']):
        next_action = 'request_human'
        transfer_reason = 'after_sales_or_dispute'
    if next_action == 'request_human':
        reply += '此问题需要人工客服核实，请使用商城的联系人工入口。'
    # 最终回复只由已确认字段及固定规则构造，不承诺虚构动作。
    reply = reply[:120]
    for order in orders:
        if query.get('failure_mode') in ['logistics_timeout', 'logistics_error', 'logistics_empty'] and query_type != 'shipping':
            for shipment in order.get('shipments', []):
                shipment['logistics_status'] = 'query_failed' if query['failure_mode'] != 'logistics_empty' else 'no_records'
                shipment['latest_status_desc'] = ''
                shipment['tracking_events'] = []
                shipment['estimated_delivery_at'] = None
    result = {'schema_version': '2.1', 'is_demo': True, 'result_type': result_type,
              'reply': reply, 'query_type': query_type, 'orders': orders,
              'candidates': query.get('candidates', []), 'next_action': next_action,
              'courier_calls':query.get('courier_calls',[]),
              'reason': transfer_reason or query.get('reason', ''),
              'human_transfer': {'requested': next_action == 'request_human', 'status': 'not_connected', 'ticket_id': None},
              'human_request': {'user_message': original.strip(), 'reason': transfer_reason,
                                'order_ids': [order['order_id'] for order in orders]} if next_action == 'request_human' else None,
              'context': {'selected_order_id': orders[0]['order_id']} if len(orders) == 1 else {}}
    return {'result_json': json.dumps(result, ensure_ascii=False), 'reply': reply,
            'result_type': result_type, 'next_action': next_action}
