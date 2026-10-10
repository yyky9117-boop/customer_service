import asyncio
import copy
import unittest
from pathlib import Path

from runtime.errors import InvalidModelOutput, ServiceFailure
from runtime.recommendation import RecommendationModule
from runtime.reply import ReplyGenerator
from runtime.service import ChatService, SessionStore
from core_helpers import NOW, ScriptedModel, WorkspaceTemp, budget, catalog, constraint, context, group, patch, state, update


def reason_output(packet):
    plans = []
    for p in packet["plans"]:
        ids = [f["id"] for f in p["facts"] if f["kind"] == "packaging"]
        fallback = p["facts"][0]["id"]
        plans.append({"plan_id":p["plan_id"],"reasons":[
            {"text":"独立包装，办公室分次取用更方便。","fact_ids":ids[:1] or [fallback]},
            {"text":"按需要的数量搭配，方便控制购买量。","fact_ids":[fallback]}]})
    return {"plans":plans}


def template_output(packet):
    lines = ["亲～，按{{need_summary}}，我建议优先考虑下面这些选择😊"]
    for n in range(1,len(packet["plans"])+1):
        lines.append(("首选" if n==1 else "备选") + "{{p"+str(n)+"_items}}，{{p"+str(n)+"_reason_1}}；{{p"+str(n)+"_price}}。")
        for suffix in ("notice","offer","difference"):
            key=f"p{n}_{suffix}"
            if key in packet["tokens"]:
                lines.append("{{"+key+"}}")
    lines.append("{{follow_up}}")
    return {"template":"\n\n".join(lines)}


class PresentationModel:
    def __init__(self, bad_reasons=False, rejects=0):
        self.calls=[];self.bad_reasons=bad_reasons;self.rejects=rejects
    async def generate_reasons(self, payload, repair=None):
        self.calls.append(('reasons',repair))
        output=reason_output(payload)
        if self.bad_reasons:
            output['plans'][0]['reasons'][0]['fact_ids']=['made_up_fact']
        return output
    async def compose_reply(self,payload,repair=None):
        self.calls.append(('reply',repair));return template_output(payload)
    async def review_reply(self,payload):
        self.calls.append(('review',None))
        if self.rejects:
            self.rejects-=1
            return {'pass':False,'issues':['选择建议语气不够自然'],'rewrite_instruction':'用更自然的建议表达重写。'}
        return {'pass':True,'issues':[],'rewrite_instruction':''}


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog=catalog(subset=['P001','P019'])
        self.needs=state(group([constraint('package','wrapped')],budget(3000,scope='order_total')))
        self.ctx=context(self.needs)
        self.core=RecommendationModule(self.catalog,None).recommend(self.needs,self.ctx)
        self.model=PresentationModel()
        self.generator=ReplyGenerator(self.model,self.catalog)

    def packet(self):
        p=self.generator.packet(self.core,self.ctx)
        for n,plan in enumerate(reason_output(p)['plans'],1):
            for index,reason in enumerate(plan['reasons'],1):
                p['tokens'][f'p{n}_reason_{index}']=reason['text'].rstrip('。')
        return p

    async def test_distinct_reason_composition_and_review_stages(self):
        out=await self.generator.enrich(self.core,self.ctx)
        self.assertEqual([c[0] for c in self.model.calls],['reasons','reply','review'])
        self.assertTrue(out.content.startswith('亲～'))
        self.assertEqual(out.content.count('😊'),1)
        self.assertIn('独立包装，办公室分次取用更方便',out.content)
        self.assertNotIn('根据这次需求，以下是可选方案',out.content)
        for before,after in zip(self.core.cards,out.cards):
            self.assertEqual({k:v for k,v in before.items() if k!='reasons'}, {k:v for k,v in after.items() if k!='reasons'})
            self.assertEqual(len(after['reasons']),2)
        self.assertTrue(out.debug['reply_review_passed'])

    async def test_unknown_evidence_never_generates_reply(self):
        model=PresentationModel(bad_reasons=True)
        with self.assertRaises(ServiceFailure):
            await ReplyGenerator(model,self.catalog).enrich(self.core,self.ctx)
        self.assertEqual([c[0] for c in model.calls],['reasons','reasons'])

    def test_cross_plan_evidence_is_rejected(self):
        p=self.packet();out=reason_output(p)
        out['plans'][0]['reasons'][0]['fact_ids']=[p['plans'][1]['facts'][0]['id']]
        with self.assertRaises(InvalidModelOutput):self.generator.validate_reasons(out,p)

    def test_reason_cannot_confuse_product_types_with_quantity(self):
        p=self.packet();out=reason_output(p)
        out['plans'][0]['reasons'][0]['text']='两件商品都采用独立小包装'
        with self.assertRaises(InvalidModelOutput):self.generator.validate_reasons(out,p)

    def test_unknown_placeholders_and_raw_price_are_rejected(self):
        p=self.packet()
        for extra in ('{{p1_fictional_coupon}}','现在只要0.01元'):
            out=template_output(p);out['template']+=extra
            with self.subTest(extra=extra),self.assertRaises(InvalidModelOutput):self.generator.render(out,p)

    def test_raw_new_product_and_exaggeration_are_rejected(self):
        p=self.packet()
        self.generator.catalog=catalog()
        for extra in ('原味海苔脆片','全网最低，绝对好吃'):
            out=template_output(p);out['template']+=extra
            with self.subTest(extra=extra),self.assertRaises(InvalidModelOutput):self.generator.render(out,p)

    def test_exact_budget_in_opening_is_allowed_but_false_budget_is_rejected(self):
        p=self.packet();out=template_output(p)
        out['template']=out['template'].replace('{{need_summary}}','含运费最多30元')
        self.assertIn('30元',self.generator.render(out,p))
        out['template']=out['template'].replace('30元','99元')
        with self.assertRaises(InvalidModelOutput):self.generator.render(out,p)

    def test_missing_reason_or_reordered_plans_are_rejected(self):
        p=self.packet();out=template_output(p)
        out['template']=out['template'].replace('{{p1_reason_1}}','挺合适的')
        with self.assertRaises(InvalidModelOutput):self.generator.render(out,p)
        out=template_output(p)
        out['template']=out['template'].replace('{{p1_items}}','TEMP').replace('{{p2_items}}','{{p1_items}}').replace('TEMP','{{p2_items}}')
        with self.assertRaises(InvalidModelOutput):self.generator.render(out,p)

    async def test_quality_rejection_gets_one_rewrite(self):
        model=PresentationModel(rejects=1)
        out=await ReplyGenerator(model,self.catalog).enrich(self.core,self.ctx)
        self.assertEqual([c[0] for c in model.calls],['reasons','reply','review','reply','review'])
        self.assertIn('更自然',model.calls[3][1])
        self.assertEqual(out.debug['reply_attempts'],2)

    async def test_persistent_quality_failure_has_no_commit(self):
        with WorkspaceTemp() as temp:
            store=SessionStore(Path(temp)/'test.sqlite3')
            needs=patch([update(constraints=[constraint('package','wrapped')])])
            service=ChatService(RecommendationModule(self.catalog,ScriptedModel(needs)),store,clock=lambda:NOW,
                reply_generator=ReplyGenerator(PresentationModel(rejects=2),self.catalog))
            request={'schema_version':'1.0','conversation_id':None,'request_id':'req_reply','expected_revision':0,'message':'测试需求','selected_reference':None}
            out=await service.handle(request)
            self.assertEqual(out['status'],'error')
            self.assertEqual(out['revision'],0)
            self.assertFalse(out['message']['cards'])
            self.assertIsNone(store.cached('local_demo','new','req_reply'))

    async def test_success_is_committed_and_retry_does_not_regenerate(self):
        with WorkspaceTemp() as temp:
            store=SessionStore(Path(temp)/'test.sqlite3')
            model=PresentationModel()
            module=RecommendationModule(self.catalog,ScriptedModel(patch([update(constraints=[constraint('package','wrapped')])])))
            service=ChatService(module,store,clock=lambda:NOW,reply_generator=ReplyGenerator(model,self.catalog))
            request={'schema_version':'1.0','conversation_id':None,'request_id':'req_reply','expected_revision':0,'message':'测试需求','selected_reference':None}
            out=await service.handle(request)
            self.assertEqual(out['status'],'completed')
            self.assertEqual(await service.handle(request),out)
            self.assertEqual(len(model.calls),3)
            self.assertEqual(store.session(out['conversation_id'],'local_demo')[1][-1]['content'],out['message']['content'])

    def test_near_expiry_never_enters_reply_generation(self):
        c=catalog(subset=['P024'])
        needs=state(group([constraint('other','sku:P024')]))
        core=RecommendationModule(c,None).recommend(needs,context(needs))
        self.assertEqual(core.status, 'no_match')
        self.assertFalse(core.cards)

    def test_unknown_shipping_cannot_be_rendered_as_total(self):
        needs=state(group([constraint('other','sku:P001')],budget(2000)))
        core=RecommendationModule(self.catalog,None).recommend(needs,context(needs,uid='U_UNKNOWN'))
        p=self.generator.packet(core,context(needs,uid='U_UNKNOWN'))
        self.assertIn('商品应付',p['tokens']['p1_price'])
        self.assertIn('运费和整单金额待确认',p['tokens']['p1_price'])
        self.assertIn('配送区域',p['tokens']['p1_notice'])

    async def test_generation_timeout_is_explicit_failure(self):
        class Waiting(PresentationModel):
            async def generate_reasons(self,*args,**kwargs):await asyncio.Event().wait()
        with self.assertRaises(ServiceFailure) as exc:
            await ReplyGenerator(Waiting(),self.catalog,timeout_seconds=.02).enrich(self.core,self.ctx)
        self.assertEqual(exc.exception.code,'model_timeout')
