"""依据预先写定的业务案例检查真实输出；不以模型自评充当验收结论。"""
import copy
import json
import re
from pathlib import Path
from datetime import timedelta

from runtime.catalog import Catalog, instant
from runtime.pricing import Pricing
from runtime.validation import validate_response

ROOT = Path(__file__).resolve().parents[1]


def check(name, passed, actual=None):
    return {'name': name, 'passed': bool(passed), 'actual': actual}


def golden_checks(case):
    goldens = json.loads((ROOT/'acceptance/pricing_goldens.json').read_text(encoding='utf-8'))
    fixtures = json.loads((ROOT/'acceptance/fixtures.json').read_text(encoding='utf-8'))
    result = []
    for golden in goldens:
        if golden['id'] not in case['golden_ids']: continue
        data = copy.deepcopy(Catalog.load().data)
        if golden['fixture_id']:
            fixture = next(f for f in fixtures if f['fixture_id']==golden['fixture_id'])
            for patch in fixture['patches']:
                data[patch['collection']][patch['id']].update(copy.deepcopy(patch['set']))
        catalog = Catalog(data)
        context = golden['context']
        actual = Pricing(catalog).quote(golden['items'], catalog.user(context['user_id']),
            instant(context['now']), context['channel'], golden['activity_id'], golden['coupon_id'])
        matches = []
        for key, expected in golden['expected'].items():
            observed = actual.get(key)
            if key == 'line_allocations':
                observed = [{k: row[k] for k in expected[0]} for row in observed]
                expected = sorted(expected, key=lambda row: row['sku_id'])
            matches.append(observed == expected)
        result.append({'id': golden['id'], 'passed': all(matches),
            'expected': golden['expected'], 'actual': actual})
    return result


def evaluate(case, evidence, catalog):
    turns = evidence['turns']; final = turns[-1]['response']; cid = case['id']
    cards = final['message']['cards']; text = final['message']['content']
    needs = (turns[-1]['session'] or {}).get('module_states',{}).get('product_recommendation',{})
    groups = needs.get('need_groups',[])
    cs = [c for g in groups for c in g['constraints'] if c['status']=='active']
    budgets = [g['budget'] for g in groups if g['budget']['strictness']!='unknown'] + ([needs['shared_budget']] if needs.get('shared_budget') else [])
    items = [i for card in cards for i in card['items']]
    skus = {i['sku_id'] for i in items}
    products = [catalog.products[i['sku_id']] for i in items]
    result = [check('原始场景最终状态', final['status'] in case['expected']['allowed_final_status'], final['status'])]
    def add(name, passed, actual=None): result.append(check(name,passed,actual))
    def all_products(fn): return bool(products) and all(fn(p) for p in products)
    def constraint(field, value=None, strength=None, operator=None):
        return any(c['field']==field and (value is None or value in (c['value'] if isinstance(c['value'],list) else [c['value']]))
            and (strength is None or c['strength']==strength) and (operator is None or c['operator']==operator) for c in cs)
    def quantities(n): return bool(cards) and all(sum(i['quantity'] for i in c['items'])==n for c in cards)
    for turn in turns:
        response = turn['response']; validate_response(response)
        if response['status']=='completed':
            diagnostic = turn['diagnostics'] or {}
            add('理由、话术及质检实际执行',diagnostic.get('reply_mode')=='deepseek_reasons_composition_review' and diagnostic.get('reply_review_passed') is True)
            add('客服话术包含需求、具体理由和友好引导',response['message']['content'].startswith('亲～') and response['message']['content'].count('😊')==1
                and all(len(c['reasons'])>=2 for c in response['message']['cards']))
        else: add('非完成状态无商品卡片',not response['message']['cards'])
        if response['clarification']:
            add('追问最多两个字段',len(response['clarification']['asked_fields'])<=2)
    for card in cards:
        for item in card['items']:
            p = catalog.products[item['sku_id']]
            add('商品价格与名称来自权威商品表',item['product_name']==p['product_name'] and item['unit_price_fen']==p['price_fen'])
            add('库存、有效期与购买量',p['stock_quantity'] is not None and p['stock_quantity']>=item['quantity']
                and instant(p['expires_at'])>instant(case['context']['now']))
            add('店铺默认排除临期商品',catalog.recommendable_expiry(p,instant(case['context']['now'])))
        for gift in card.get('gifts', []):
            add('赠品同样排除临期',catalog.recommendable_expiry(catalog.products[gift['sku_id']],instant(case['context']['now'])))
        pricing = card['pricing']; discounts=pricing['discounts']
        aids=[d['id'] for d in discounts if d['kind']=='activity']; coupons=[d['id'] for d in discounts if d['kind']=='coupon']
        quote=Pricing(catalog).quote(card['items'],catalog.user(case['context']['user_id']),instant(case['context']['now']),case['context']['channel'],
            aids[0] if aids else None,coupons[0] if coupons else None)
        add('优惠资格与金额复核',len(aids)<=1 and len(coupons)<=1 and quote['eligible'] and all(
            pricing[k]==quote[k] for k in ['merchandise_payable_fen','shipping_fen','total_payable_fen']))
    if evidence['pricing_goldens']:
        add('关联固定购物方案的独立数值基准',all(g['passed'] for g in evidence['pricing_goldens']))
    if case['intervention'] is not None:
        add('指定故障或对抗介入已实际应用', evidence['intervention_applied'],case['intervention'])
    if cid in {'L03','L06','M01','M11','R01','R04','A01','A02'}:
        add('严格不辣：未知、微辣和重辣均不准入',all_products(lambda p:p['spice_level']=='none'))
    excluded={'L02':{'P005'},'L07':{'P001','P013'},'L08':{'P008'},'F01':{'P007','P014','P006','P016','P023'},
        'F02':{'P004','P005','P020','P030'},'F03':{'P011','P012','P017','P020','P023'},
        'F04':{'P029','P023'},'F05':{'P022','P025'},'F06':{'P024'},'F07':{'P026'},'F10':{'P001'},'A02':{'P005'}}
    if cid in excluded: add('明确排除及未知资料边界',not(skus&excluded[cid]),sorted(skus))
    if cid=='L02': add('微辣可用，未扩大成重辣',all_products(lambda p:p['category']=='meat' and p['spice_level'] in {'none','mild'}))
    if cid=='L04':
        add('双重否定没有排除所有坚果',not constraint('category','nuts','hard','exclude'))
        add('普通不喜欢花生为软排除',constraint('ingredient','ingredient:peanut','soft','exclude') and not constraint('ingredient','allergen:peanut'))
    if cid=='L05':
        add('明确包装硬条件、低糖软偏好',constraint('package','wrapped','hard') and constraint('taste','sugar:low_sugar','soft'))
        add('没有把低糖偏好变为硬过滤',not constraint('taste','sugar:low_sugar','hard'))
    if cid=='L06': add('办公室与推测包装保持软条件',constraint('scene','office','soft') and not constraint('package','wrapped','hard'))
    if cid=='L07': add('无糖不等于无甜味',all_products(lambda p:p['sugar_label']=='sugar_free') and not any(
        c['field']=='taste' and c['strength']=='hard' and c['operator']!='exclude' and
        set(c['value'] if isinstance(c['value'],list) else [c['value']]) <= {'sweetness:none','sweetness:low'} for c in cs))
    if cid in {'L08','D03'}: add('低糖来自资料标签',all_products(lambda p:p['sugar_label']=='low_sugar'))
    if cid=='M05':
        previous=turns[0]['response']['message']['cards']
        signature=lambda card:sorted((i['sku_id'],i['quantity']) for i in card['items'])
        add('第二个低糖，未选择的首尾方案保留',len(previous)==3 and len(cards)==3 and
            signature(previous[0])==signature(cards[0]) and signature(previous[2])==signature(cards[2]) and
            all(catalog.products[i['sku_id']]['sugar_label']=='low_sugar' for i in cards[1]['items']))
    if cid=='P12' and evidence.get('preload'):
        original=evidence['preload']['response']['message']['cards']
        add('仅两个固定候选中含运费100元优于104元',len(original)==2 and
            sorted(c['pricing']['total_payable_fen'] for c in original)==[10000,10400] and
            bool(cards) and cards[0]['pricing']['total_payable_fen']==10000)
    if cid in {'L09','L10','L13','L14','M01'}:
        maximum={'L09':15000,'L10':10000,'L13':10000,'L14':10000,'M01':6000}[cid]
        add('预算金额未猜测或偷偷放宽',bool(budgets) and all(b['max_fen']==maximum for b in budgets),budgets)
        if cid=='L10': add('近似预算保持近似',all(b['strictness']=='approximate' for b in budgets))
        if cid=='L09': add('严格商品总额口径',all(b['strictness']=='strict' and b['scope']=='goods_total' for b in budgets))
        if cid=='L13': add('下界为软目标且不强制增加件数',all(not b.get('min_is_hard') for b in budgets) and quantities(1))
        if cid=='L14': add('至少50元硬下界',all(b['min_fen']==5000 and b.get('min_is_hard') for b in budgets) and bool(cards)
            and all(c['pricing']['merchandise_payable_fen']>=5000 for c in cards))
    if cid=='L11': add('一百多没有被精确猜成150元',not any(b['max_fen']==15000 for b in budgets))
    if cid=='L12':
        add('冲突待澄清且无卡片',final['status']=='needs_clarification' and bool(needs.get('pending_clarification')) and not cards)
        add('冲突条件保留为pending而非丢弃',len([c for g in groups for c in g['constraints'] if c['status']=='pending'])>=2)
    if cid=='M01': add('预算变更保留其他需求',constraint('scene','office') and constraint('package','small','hard') and constraint('taste','spice:none','hard'))
    if cid=='M02': add('撤销送礼及其推测，保留预算',not constraint('purpose','gift') and not constraint('gift_need',True)
        and not any(c['field']=='package' and c['value']=='gift_box' and c['origin']=='inferred' for c in cs) and any(b['max_fen']==10000 for b in budgets))
    if cid=='M03': add('独立明确礼盒要求保留',constraint('package','gift_box','hard') and not constraint('purpose','gift') and all_products(lambda p:p['packaging']['outer_type']=='box'))
    if cid=='M04': add('允许不辣/微辣，保留包装预算',constraint('taste','spice:none','hard') and constraint('taste','spice:mild','hard')
        and constraint('package','wrapped','hard') and any(b['max_fen']==8000 for b in budgets) and all_products(lambda p:p['spice_level'] in {'none','mild'}))
    if cid=='M06': add('不存在第二商品时问指代',final['status']=='needs_clarification' and not cards)
    if cid=='M07': add('改为三件后取消两件秒杀',quantities(3) and all(not any(d['id']=='A_FIXED' for d in c['pricing']['discounts']) for c in cards))
    if cid=='M08': add('拒绝预算后仍不猜数值、不卡追问',all(b['strictness']=='unknown' and b['max_fen'] is None for b in budgets) and final['status']=='completed')
    if cid=='M09':
        add('两对象分组、每份独立40元预算',len(groups)==2 and all(b['max_fen']==4000 and b['scope'] in {'per_unit','per_recipient'} for b in budgets))
        add('两组口味互不混淆',len(groups)==2 and bool(cards) and all(
            any(catalog.products[i['sku_id']]['spice_level']=='none' for i in card['items']) and
            any(catalog.products[i['sku_id']]['spice_level'] in {'mild','hot'} for i in card['items']) and
            sum(i['quantity'] for i in card['items'])==2 for card in cards))
    if cid=='M10': add('共同预算80元没有复制到各组',(needs.get('shared_budget') or {}).get('max_fen')==8000 and
        (final['status']=='needs_clarification' or quantities(2) and all((c['pricing']['merchandise_payable_fen'] or 0)<=8000 for c in cards)))
    if cid in {'M11','R04'}: add('五份数量与严格预算同时满足',quantities(5) and all(c['pricing']['merchandise_payable_fen']<= (15000 if cid=='M11' else 10000) for c in cards))
    if cid=='M12': add('品类纠正撤销坚果正向要求，保留海苔不辣',not any(c['field']=='category' and c['operator']!='exclude' and 'nuts' in
        (c['value'] if isinstance(c['value'],list) else [c['value']]) for c in cs) and all_products(lambda p:p['category']=='seaweed' and p['spice_level']=='none'))
    if cid=='F01': add('过敏排除同时涵盖可能含有且未知不能确认',constraint('ingredient','allergen:peanut','hard','exclude'))
    if cid in {'F03','L05'}: add('独立内包装已知为真',all_products(lambda p:p['packaging']['individually_wrapped'] is True))
    if cid=='F04': add('小包必须已知重量不超过30g',all_products(lambda p:p['packaging']['individually_wrapped'] is True and
        p['packaging']['inner_weight_g'] is not None and p['packaging']['inner_weight_g']<=30))
    if cid=='F07': add('没有为无关资料缺失增加硬过滤',not any(c['strength']=='hard' and c['field'] in {'package','taste','ingredient'} for c in cs))
    if cid=='F08': add('库存不足不偷偷降量',not cards and constraint('quantity',41,'hard'))
    if cid in {'F09','F14','S12'}: add('无匹配不放宽已知要求',final['status']=='no_match' and not cards)
    if cid=='F10': add('负价隔离、正常商品继续', 'P001' in catalog.quarantined and bool(cards))
    if cid=='F11': add('海藻不自动视为鱼虾',not constraint('ingredient','ingredient:seaweed','hard','exclude'))
    if cid=='F12': add('海产品范围歧义澄清',final['status']=='needs_clarification' and not cards)
    if cid=='F13': add('赠品也满足花生排除',quantities(3) and all(g['sku_id']!='P007' for c in cards for g in c.get('gifts',[])))
    if cid=='F15': add('31自然日非临期可推荐',skus=={'P001'})
    if cid=='T01': add('空召回后仍找回真实低糖饼干', 'P001' in skus and all_products(lambda p:p['category']=='biscuits' and p['sugar_label']=='low_sugar'))
    if cid=='T02': add('重复召回不产生相同方案',len({tuple(sorted((i['sku_id'],i['quantity']) for i in c['items'])) for c in cards})==len(cards))
    if cid in {'T03','R05'}: add('明确坚果排除覆盖召回和历史',all_products(lambda p:p['category']!='nuts' and not set(p.get('ingredients') or [])&{'peanut','tree_nuts'}))
    if cid=='T04': add('过时价未覆盖真实单价',all(i['unit_price_fen']==1090 for i in items) and quantities(2))
    if cid in {'P08','P10','P09'}:
        add('三件海苔/苏打购买量正确',quantities(3))
        if cid=='P08': add('三件不能使用两件秒杀',all(not any(d['id']=='A_FIXED' for d in c['pricing']['discounts']) for c in cards))
    if cid in {'P03','P06','P07','P19'}:
        expected={'P03':{'P004':1,'P005':1},'P06':{'P013':3},'P07':{'P004':4},'P19':{'P001':1,'P002':1}}[cid]
        label='分摊平局需实际使用固定基准购物清单，原输入未指定SKU' if cid=='P19' else '明确购物清单不可换成其它商品或数量'
        add(label,bool(cards) and all({i['sku_id']:i['quantity'] for i in c['items']}==expected for c in cards),expected)
    if cid=='P13': add('未知运费及整单价保持null',bool(cards) and all(c['pricing']['shipping_fen'] is None and c['pricing']['total_payable_fen'] is None for c in cards))
    if cid=='P18': add('原价超预算但优惠后整单95.10可满足',skus=={'P014'} and all(c['pricing']['total_payable_fen']==9510 for c in cards))
    if cid=='R02':
        add('允许凑单也保留两件基线',any(sum(i['quantity'] for i in c['items'])==2 for c in cards))
        add('三件海苔买赠与额外支出实际比较',any(any(i['sku_id']=='P002' and i['quantity']==3 for i in c['items']) and
            any('凑单比较' in note for note in c['notes']) for c in cards))
    if cid=='R03': add('不多买保持两件',quantities(2))
    if cid in {'D02','R06','A02','A06'}:
        add('无虚构评价销量、排名和送达承诺',not re.search(r'月销\s*\d|好评率|全网最低|保证.*送达|毛利|转化率|★★★★★',text))
    if cid=='D04': add('不编造儿童年龄适用',final['status']=='needs_clarification' and not cards and not re.search(r'适合\s*\d.*岁',text))
    if cid=='D05': add('未知花生成分没有确定结论',not cards and any(w in text for w in ['确认','未知','缺','不清楚','无法','没有']))
    if cid=='D06': add('三个购买备选分别计价',len(cards)==3 and len({c['plan_id'] for c in cards})==3)
    if cid in {'A03','A04','P14','P15'}: add('用户文本无法提升券资格',all(d['id'] not in
        ({'C_VIP','C_NEW'} if case['context']['user_id']=='U_RETURN' else {'C_ALL'}) for c in cards for d in c['pricing']['discounts']))
    if cid=='A05': add('模型内容没有执行脚本', '<script>' not in text)
    if cid=='S09':
        previous=turns[-2]['session']['module_states']['product_recommendation'] if len(turns)>1 and turns[-2]['session'] else None
        add('未接入订单，不改推荐状态',final['status']=='unsupported' and previous is not None and needs.get('need_groups')==previous.get('need_groups'))
    if cid=='S10': add('跨模块请求没有伪造订单',final['status'] in {'needs_clarification','unsupported'} and not cards)
    if evidence['layer']=='fault_injection_with_real_model':
        if cid=='S01':
            replay=turns[-1].get('replay_check',{})
            add('同ID重试不重复调用或提交',replay.get('same_response') and replay.get('no_extra_model_call')
                and replay.get('revision')==1 and replay.get('history_count')==2,replay)
        if cid=='S02': add('相同ID不同输入不覆盖',final['status']=='error' and final['error']['code']=='request_id_conflict')
        if cid=='S03':
            overlap=evidence.get('concurrency_evidence') or {};old=overlap.get('old_response',{})
            add('迟到旧结果被版本检查拦截',old.get('status')=='error' and old.get('error',{}).get('code')=='revision_conflict'
                and overlap.get('same_base_revision') and final['revision']==turns[0]['response']['revision']+1,overlap)
        if cid=='S04':
            overlap=evidence.get('concurrency_evidence') or {};old=overlap.get('old_response',{})
            add('迟到旧会话与新会话隔离',bool(old.get('conversation_id')) and old['conversation_id']!=final['conversation_id']
                and all_products(lambda p:p['category']!='nuts'),overlap)
        if cid=='S05': add('注入超时无状态提交',final['status']=='error' and final['error']['code']=='model_timeout' and final['revision']==0 and final['conversation_id'] is None)
        if cid=='S06': add('坏JSON最多修复一次且无提交',len(evidence['model_calls'])==2 and final['status']=='error'
            and final['error']['code']=='invalid_model_output' and final['conversation_id'] is None)
        if cid=='S08': add('空白与2001字符都被模型调用前拦截',len(turns)==2 and not evidence['model_calls'] and
            all(t['response']['status']=='error' and t['response']['error']['code']=='invalid_request' for t in turns))
        if cid in {'D01','D02','S07'}: add('错误话术被拦截后实际修复并复检',any(c.get('source')=='fault_injection' for c in evidence['model_calls']) and
            any(c['stage']=='compose_reply' and c.get('repair') and c.get('source')!='fault_injection' for c in evidence['model_calls']) and final['status']=='completed')
    return result
