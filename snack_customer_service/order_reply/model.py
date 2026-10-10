"""订单模块独立的话术模型接口，沿用已有本机 DeepSeek 配置。"""
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

from model_adapter import ModelFailure, NoRedirect

ROOT = Path(__file__).resolve().parent


class ReplyModel:
    def __init__(self, extractor, opener=None):
        self.extractor = extractor
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    @property
    def configured(self):
        return self.extractor.configured

    def call(self, prompt, payload, repair=None):
        if not self.configured:
            raise ModelFailure('not_configured')
        system = (ROOT / 'prompts' / (prompt+'.md')).read_text(encoding='utf-8')
        messages = [{'role':'system','content':system},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
        if repair:
            messages.append({'role':'user','content':'上一次输出未通过校验，请按事实资料修复：'+repair[:900]})
        body = {'model':self.extractor.model,'messages':messages,'response_format':{'type':'json_object'},
            'max_tokens':1400,'thinking':{'type':'disabled'},'stream':False}
        request = urllib.request.Request(self.extractor.url,data=json.dumps(body,ensure_ascii=False).encode('utf-8'),
            headers={'Authorization':'Bearer '+self.extractor.key,'Content-Type':'application/json'},method='POST')
        try:
            with self.opener.open(request,timeout=self.extractor.timeout) as response:
                raw=response.read(131073)
            if len(raw)>131072:raise ModelFailure('invalid_output')
            choice=json.loads(raw)['choices'][0]
            if choice.get('finish_reason')!='stop':raise ModelFailure('incomplete_output')
            output=json.loads(choice['message']['content'])
            if not isinstance(output,dict):raise ModelFailure('invalid_output')
            return output
        except urllib.error.HTTPError as exc:
            raise ModelFailure('authentication_failed' if exc.code in (401,403) else 'rate_limited' if exc.code==429 else 'provider_error') from None
        except (urllib.error.URLError,TimeoutError,socket.timeout,OSError):
            raise ModelFailure('connection_failed') from None
        except (ValueError,KeyError,IndexError,TypeError,UnicodeError):
            raise ModelFailure('invalid_output') from None
