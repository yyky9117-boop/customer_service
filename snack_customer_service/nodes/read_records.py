import json


async def main(args):
    p = args.params
    records = p.get('records')
    success = p.get('isSuccess') is not False and isinstance(records, list)
    return {'data_json': json.dumps(records if success else [], ensure_ascii=False),
            'source_is_success': success,
            'source_may_be_truncated': success and len(records) >= 100}
