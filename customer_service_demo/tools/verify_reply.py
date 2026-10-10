"""核对话术优化的测试和真实浏览器证据，不重新调用模型。"""
import io
import json
import sqlite3
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


class Evidence(unittest.TextTestResult):
    def __init__(self,*args):
        super().__init__(*args);self.executed=[]
    def startTest(self,test):
        self.executed.append(test.id());super().startTest(test)


def verify():
    stream=io.StringIO()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(stream=stream,verbosity=0,resultclass=Evidence).run(suite)
    source=ROOT/'acceptance/stage4-browser-reply.json'
    browser=json.loads(source.read_text(encoding='utf-8')) if source.exists() else {'all_passed':False}
    requests=next((c['detail'] for c in browser.get('checks',[]) if c['name']=='real_requests_for_server_pipeline_audit'),[])
    audits=[]
    with sqlite3.connect(ROOT/'.local/web-sessions.sqlite3') as db:
        for request in requests:
            rows=db.execute('SELECT debug FROM requests WHERE request_id=? AND scope=?',(request['request_id'],request['scope'])).fetchall()
            for row in rows:
                diagnostic=json.loads(row[0])
                audits.append({'request_id':request['request_id'],'reply_mode':diagnostic.get('reply_mode'),
                    'reason_attempts':diagnostic.get('reason_attempts'),'reply_attempts':diagnostic.get('reply_attempts'),
                    'review_passed':diagnostic.get('reply_review_passed',False)})
    complete=bool(audits) and len(audits)==len({r['request_id'] for r in requests}) and all(
        a['reply_mode']=='deepseek_reasons_composition_review' and a['review_passed'] for a in audits)
    stage5_path=ROOT/'acceptance/stage5_execution.json'
    stage5=json.loads(stage5_path.read_text(encoding='utf-8')) if stage5_path.exists() else None
    report={'verified_at':datetime.now(timezone.utc).isoformat(),'all_passed':result.wasSuccessful() and browser['all_passed'] and complete,
        'automated_tests':{'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'by_module':dict(sorted(Counter(t.split('.')[0] for t in result.executed).items()))},
        'production_pricing_goldens':53,'real_browser_evidence':str(source.relative_to(ROOT)).replace('\\','/'),
        'browser_verified_at':browser.get('verified_at'),'live_pipeline_audit':audits,
        'natural_language_accuracy':None,'98_full_business_scenarios':stage5['decision'] if stage5 else 'not_fully_executed',
        'stage5_evidence':'acceptance/stage5_execution.json' if stage5 else None,'executed_test_ids':result.executed}
    (ROOT/'acceptance/reply_execution.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='executed_test_ids'},ensure_ascii=False,indent=2))
    if not result.wasSuccessful():print(stream.getvalue())
    return 0 if report['all_passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(verify())
