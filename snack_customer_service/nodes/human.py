import json


async def main(args):
    message = str(args.params.get('USER_INPUT') or '')
    result = {'schema_version': '2.0', 'is_demo': True, 'result_type': 'human_required',
              'reply': '此问题需要人工客服处理，请使用商城的联系人工入口。',
              'orders': [], 'candidates': [], 'next_action': 'request_human',
              'reason': 'unsupported_intent',
              'human_transfer': {'requested': True, 'status': 'not_connected', 'ticket_id': None},
              'human_request': {'user_message': message, 'reason': 'unsupported_intent', 'order_ids': []},
              'context': {}}
    return {'result_json': json.dumps(result, ensure_ascii=False), 'reply': result['reply'],
            'result_type': result['result_type'], 'next_action': result['next_action']}
