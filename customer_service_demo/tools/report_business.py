"""汇总已存在的真实证据，不调用模型，不把未执行或失败案例标成通过。"""
import json
import hashlib
import sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from accept_business import fingerprint,fixture_catalog,read
from business_oracles import evaluate,golden_checks


def main():
    revision=fingerprint(); folder=ROOT/'acceptance/stage5'/revision
    cases=read('acceptance/cases.json')
    browser=read('acceptance/stage5-browser.json')
    results=[]; classifications=Counter()
    for case in cases:
        records=[]; faults=[]
        for repetition in range(1,case['repeat_count']+1):
            for fault in [False,True]:
                path=folder/f"{case['id']}-{'fault-' if fault else ''}r{repetition}.json"
                if not path.exists():continue
                evidence=json.loads(path.read_text(encoding='utf-8'))
                checks=evaluate(case,evidence,fixture_catalog(case))
                record={'repetition':repetition,'passed':all(c['passed'] for c in checks),
                    'status':evidence['turns'][-1]['response']['status'],
                    'failed_checks':[c['name'] for c in checks if not c['passed']],
                    'evidence':str(path.relative_to(ROOT)).replace('\\','/'),
                    'error_codes':[t['response']['error']['code'] for t in evidence['turns'] if t['response']['error']]}
                (faults if fault else records).append(record)
        completed=len(records)==case['repeat_count']
        needs_fault=case['intervention'] is not None or case['fixture_id'] in {'F_FALSE_FACT','F_PRICE_TEXT_MISMATCH','F_INJECTION_DOC','F_DUPLICATE_SKU'}
        primary=faults if needs_fault and faults else records
        passed=completed and len(primary)==case['repeat_count'] and all(r['passed'] for r in primary)
        if case['id']=='S11': passed=completed and browser['all_passed'] and any(c['name'].startswith('S11_') for c in browser['checks'])
        if case['id']=='S04': passed=passed and browser['all_passed'] and any(c['name'].startswith('S04_') for c in browser['checks'])
        if needs_fault and not faults and case['id']!='S11': passed=False
        findings=[]
        if not passed:
            for record in primary:
                if record['passed']:continue
                if 'data_unavailable' in record['error_codes']: findings.append('模型连接或服务失败')
                elif 'invalid_model_output' in record['error_codes']:findings.append('需求/话术校验未通过')
                elif record['status']=='unsupported':findings.append('商品/优惠咨询未完整支持')
                elif record['status']=='needs_clarification':findings.append('澄清与原预期不一致，需检查歧义或搜索上限')
                elif case['id']=='P19':findings.append('固定数值基准所需的SKU前置条件未写入原输入')
                else:findings.append('业务条件或推荐结果未满足')
            if needs_fault and not faults and case['id']!='S11':findings.append('指定介入尚未形成完整证据')
            if not completed:findings.append('规定重复次数未完成')
        classifications.update(set(findings))
        row={'case_id':case['id'],'title':case['title'],'priority':case['priority'],'required_repetitions':case['repeat_count'],
            'executed':bool(records),'required_repetitions_completed':completed,'passed':passed,
            'original_expected_status':case['expected']['allowed_final_status'],
            'natural_service_runs':records,'fault_runs':faults,'findings':sorted(set(findings))}
        results.append(row)
        if records:
            case['executed']=True;case['execution_evidence']=records[0]['evidence'];case['execution_stage']='stage5'
            case['execution_result']='passed' if passed else 'not_passed'
    quote_results=golden_checks({'golden_ids':[g['id'] for g in read('acceptance/pricing_goldens.json')]})
    report={'recorded_at':datetime.now(timezone.utc).isoformat(),'implementation_hash':revision,
        'oracle_hash':hashlib.sha256((ROOT/'tools/business_oracles.py').read_bytes()).hexdigest(),
        'decision':'passed' if all(r['passed'] for r in results) and browser['all_passed'] else 'not_passed',
        'business_cases':{'total':len(results),'executed':sum(r['executed'] for r in results),
            'required_repetitions_completed':sum(r['required_repetitions_completed'] for r in results),
            'passed':sum(r['passed'] for r in results),'not_passed':sum(not r['passed'] for r in results),
            'natural_service_runs':sum(len(r['natural_service_runs']) for r in results),
            'fault_runs':sum(len(r['fault_runs']) for r in results)},
        'failure_categories':dict(classifications),'pricing_goldens':{'total':53,'passed':sum(g['passed'] for g in quote_results)},
        'browser':{'all_passed':browser['all_passed'],'evidence':'acceptance/stage5-browser.json'},
        'natural_language_accuracy':None,
        'limitations':['场景通过指本工具所列状态与业务断言通过，不是完整自然语言语义的确定性证明。',
            '所有固定计价基准与自然对话分别记录，不互相替代；模型质检也不视作独立人工验收。',
            'P0失败不能用平均通过率抵消；原案例预期保持，不为通过而放宽。'],
        'cases':results}
    verification_path=ROOT/'acceptance/stage5-code-tests.json'
    if verification_path.exists():
        verification=json.loads(verification_path.read_text(encoding='utf-8'))
        if verification['implementation_hash']==revision:
            report['code_verification']={k:verification[k] for k in ['automated_tests','execution_registry_consistent','report_matches_current_code']}
            report['code_verification']['evidence']='acceptance/stage5-code-tests.json'
    (ROOT/'acceptance/stage5_execution.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'acceptance/stage5-pricing.json').write_text(json.dumps(quote_results,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'acceptance/cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    coverage=read('acceptance/coverage.json');coverage['all_cases_executed']=all(r['executed'] for r in results)
    coverage['all_cases_passed']=all(r['passed'] for r in results);coverage['execution_evidence']='acceptance/stage5_execution.json'
    coverage['note']='第五步执行状态见逐项真实证据；已执行不代表验收通过。'
    (ROOT/'acceptance/coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    rows=['# 第五步业务验收记录','','**结论：'+('通过。' if report['decision']=='passed' else '未通过整体验收。')+'**','',
        '用户流程：在客服界面输入需求，经真实 DeepSeek 拆解、本地商品与优惠查询、确定性筛选计价，再生成理由、客服话术并质检。',
        '',f"98场景执行 {report['business_cases']['executed']} 项，规定重复次数完成 {report['business_cases']['required_repetitions_completed']} 项；场景判定通过 {report['business_cases']['passed']} 项，未通过 {report['business_cases']['not_passed']} 项。",'',
        f"当前版本真实服务执行 {report['business_cases']['natural_service_runs']} 次；另有明确注入的故障执行 {report['business_cases']['fault_runs']} 次。38个高风险案例各要求5次，全部计划共250次服务执行。固定时间2026-10-07，数据全部虚构。",'',
        '**本次修复**：礼盒条件映射商品表的box；禁止小包与独立包装的或关系放宽重量限制；禁止未要求小包时擅加30g限制；补强原料前缀修复；保留仅预算需求；避免明确预算被模型遗漏；未知花生成分直接保留无法确认提示；儿童需求缺年龄时澄清；客服话术修复指出具体遗漏占位符。','',
        '**分别核验**：53个固定计价基准全部单独计算，结果见 `acceptance/stage5-pricing.json`。浏览器真实API/模型/资料/回复、320px与200%文字、迟到回复隔离见 `acceptance/stage5-browser.json`。代码测试结果另列，不能与业务场景或53基准相加。','',
        '**仍须处理**：模糊预算口径的合理澄清与原案例要求立即推荐存在冲突；五份广泛混搭可能达到5000组合上限；自由预算、多轮更新和历史第二方案指代仍有模型不稳定；独立商品成分、单券与活动咨询未形成完整对话能力；部分真实调用存在连接失败；事实/话术持续校验失败会返回错误而非保存虚构推荐。','',
        '原始预期未改动。合理追问不被宣称为完全符合原案例；单纯数值基准通过不代表对应自然语言问题已回答。需要介入的场景逐项标明证据缺口，不能在未注入时假称故障通过。','',
        '基准前置条件例子：P19输入仅“一件饼干一件海苔”，却期待P001/P002的平局分摊。实际推荐选择了其它合法饼干，不能据此指责系统违背用户指定SKU；固定G049计价已通过，但这条自然对话没有真正触发指定SKU的边界条件，完整原案例仍记未通过。','',
        '表内“通过”只表示列出的程序业务断言通过，不代表已经人工逐句认证全部自由话术。250轮为服务执行轮次，儿童/未知成分与输入校验可能在模型调用前完成。模型实际阶段输出可在每轮model_calls中核对。','',
        '置信度：高——执行次数、状态、整数计价、会话隔离与可追溯证据；中——已采样话术的语义质量；自然语言整体准确率未知，未提供该指标。','',
        '| ID | 场景 | 服务重复 | 故障重复 | 判定 | 实际状态 / 失败类别 |','|---|---|---:|---:|---|---|']
    for row in results:
        states=','.join(sorted({r['status'] for r in row['natural_service_runs']}))
        rows.append(f"| {row['case_id']} | {row['title']} | {len(row['natural_service_runs'])}/{row['required_repetitions']} | {len(row['fault_runs'])} | {'通过' if row['passed'] else '未通过'} | {states}；{'；'.join(row['findings'])} |")
    rows+=['','证据汇总：`acceptance/stage5_execution.json`。每次证据包含原始请求、实际回复、保存状态、模型阶段输出与逐项断言；密钥不记录。', '',
        '复核已保存结果：`python tools/report_business.py`。执行真实场景：`python tools/accept_business.py --repeat required`。明确故障：追加 `--faults --ids 场景ID列表`。后者按工具支持的介入执行，未支持的介入不会被标为通过。','']
    if report.get('code_verification'):
        code=report['code_verification']['automated_tests']
        rows+=['',f"最终代码回归：{code['run']}项，失败{code['failures']}项，错误{code['errors']}项；结果见 `acceptance/stage5-code-tests.json`。重新核对使用 `python tools/verify_stage5.py`，它不重新调用模型。",'']
    (ROOT/'docs/09-第五步业务验收.md').write_text('\n'.join(rows),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'cases','limitations'}},ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
