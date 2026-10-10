"""最终离线回归与第五步证据一致性检查；不消耗模型额度。"""
import hashlib
import io
import json
import sys
import unittest
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from accept_business import fingerprint


class Evidence(unittest.TextTestResult):
    def __init__(self,*args):super().__init__(*args);self.executed=[]
    def startTest(self,test):self.executed.append(test.id());super().startTest(test)


def main():
    stream=io.StringIO();suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(stream=stream,verbosity=0,resultclass=Evidence).run(suite)
    report=json.loads((ROOT/'acceptance/stage5_execution.json').read_text(encoding='utf-8'))
    cases=json.loads((ROOT/'acceptance/cases.json').read_text(encoding='utf-8'))
    consistency=[]
    for case,row in zip(cases,report['cases'],strict=True):
        correct=case['id']==row['case_id'] and len(row['natural_service_runs'])==case['repeat_count']
        for record in row['natural_service_runs']+row['fault_runs']:
            evidence=json.loads((ROOT/record['evidence']).read_text(encoding='utf-8'))
            correct &= evidence['case_id']==case['id'] and evidence['implementation_hash']==fingerprint()
        consistency.append({'case_id':case['id'],'passed':bool(correct)})
    evidence={'verified_at':datetime.now(timezone.utc).isoformat(),'implementation_hash':fingerprint(),
        'command':'python -m unittest discover -s tests -q',
        'automated_tests':{'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'passed':result.wasSuccessful(),'by_module':dict(sorted(Counter(x.split('.')[0] for x in result.executed).items()))},
        'execution_registry_consistent':all(c['passed'] for c in consistency),
        'report_matches_current_code':report['implementation_hash']==fingerprint(),
        'business_acceptance_decision':report['decision'],'cases':consistency,'executed_test_ids':result.executed}
    (ROOT/'acceptance/stage5-code-tests.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    report['code_verification']={k:evidence[k] for k in ['automated_tests','execution_registry_consistent','report_matches_current_code']}
    report['code_verification']['evidence']='acceptance/stage5-code-tests.json'
    report['oracle_hash']=hashlib.sha256((ROOT/'tools/business_oracles.py').read_bytes()).hexdigest()
    (ROOT/'acceptance/stage5_execution.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in evidence.items() if k not in {'cases','executed_test_ids'}},ensure_ascii=False,indent=2))
    if not result.wasSuccessful():print(stream.getvalue())
    return 0 if result.wasSuccessful() and evidence['execution_registry_consistent'] and evidence['report_matches_current_code'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
