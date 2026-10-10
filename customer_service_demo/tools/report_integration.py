"""汇总实际自动检查、浏览器证据和订单事实；不把旧全量结果算作本轮通过。"""
import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration.contracts import SCHEMAS


def read(path):
    return json.loads((ROOT/path).read_text(encoding='utf-8-sig'))


def main():
    log=(ROOT/'.local/unified-tests-final.log').read_text(encoding='utf-8',errors='replace')
    count=int(re.search(r'Ran (\d+) tests',log).group(1))
    passed=log.rstrip().endswith('OK')
    browser=read('acceptance/stage4-browser-orders.json')
    checks=browser['checks']
    facts=[]
    with closing(sqlite3.connect((ROOT/'.local/web-sessions.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        for c in checks:
            detail=c.get('detail')
            if not isinstance(detail,dict) or 'request_id' not in detail:continue
            rows=db.execute('SELECT response,debug FROM requests WHERE request_id=?',(detail['request_id'],)).fetchall()
            if len(rows)!=1:raise ValueError('浏览器请求证据不唯一或缺失')
            response,debug=map(json.loads,rows[0])
            trace=debug['reply_trace']
            facts.append({'check':c['name'],'request_id':detail['request_id'],'conversation_id':response['conversation_id'],
                'module':response['module'],'status':response['status'],'reply_mode':trace['mode'],
                'independent_review_passed':trace['review_passed'],'retrieval':trace['retrieval'],
                'courier_calls':[{'provider':x['provider'],'is_simulated':x['is_simulated'],'transport':x['transport'],
                    'http_status':x['http_status'],'status':x['status'],'tracking_number':x['request']['tracking_number']}
                    for x in debug.get('courier_calls',[])]})
    prior=read('.local/integrated-bootstrap-before-final-restart.json')['model_usage']
    for name,schema in SCHEMAS.items():
        (ROOT/'integration/schemas'/(name+'.schema.json')).write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    paths=[]
    for folder,suffix in [('runtime','*.py'),('integration','*.py'),('prompts','*.md'),('web','*'),('data','*.json')]:
        paths.extend(p for p in (ROOT/folder).glob(suffix) if p.is_file())
    source_hashes={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    model_verified=bool(facts) and all(f['reply_mode']=='deepseek_grounded_reply' and f['independent_review_passed'] for f in facts)
    report={'verified_at':datetime.now(timezone.utc).isoformat(),
        'scope':'订单接入、默认临期排除、会话边界；不是98场景全量复测',
        'order_integration_verified':passed and browser['all_passed'] and model_verified,
        'overall_customer_service_acceptance':'not_passed',
        'automated_tests':{'run':count,'passed':passed,'pricing_goldens':len(read('acceptance/pricing_goldens.json'))},
        'browser':{'file':'acceptance/stage4-browser-orders.json','checks':len(checks),'all_passed':browser['all_passed'],'console_errors':browser['console_errors']},
        'actual_order_reply_and_http_evidence':facts,'source_sha256':source_hashes,
        'expiry_policy':{'near_days':30,'day_30_excluded':True,'day_31_allowed_if_other_requirements_met':True,
            'applies_to':['主推','备选','混搭','凑单','赠品'],'unknown_expiry_excluded':True,'user_cannot_override':True},
        'remaining_model_failures':[
            {'file':'acceptance/integrated-browser-second-attempt.json','stage':'需求理解核验','passed':False},
            {'file':'acceptance/stage4-browser-integrated.json','stage':'推荐客服话术核验','passed':False}],
        'first_browser_attempt':'acceptance/integrated-browser-first-attempt.json',
        'historical_full_acceptance':'acceptance/stage5_execution.json',
        'tokens':{'requested_ceiling':10000000,'per_server_model_conservative_limit':500000,
            'last_test_server_usage':prior,'earlier_test_server_exact_usage_available':False,
            'earlier_test_server_conservative_upper_bound':500000,
            'live_model_conservative_upper_bound':500000+prior['charged_upper_bound'],
            'new_server_after_tests_has_no_live_model_test_calls':True,
            'agent_usage_source':'当前Codex目标账本，完成后另行记录'},
        'original_order_project_modified_by_this_integration':False,
        'source_order_database_mode':'ro','human_service_integrated':False,'logistics_source':'local_simulated_http',
        'history_migration_backup':'.local/unified-backups/'}
    (ROOT/'acceptance/order-integration-execution.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'automated_tests':report['automated_tests'],'browser':report['browser'],
        'order_integration_verified':report['order_integration_verified'],'live_model_upper_bound':report['tokens']['live_model_conservative_upper_bound']},ensure_ascii=False))
    if not report['order_integration_verified']:raise SystemExit(1)


if __name__=='__main__':main()
