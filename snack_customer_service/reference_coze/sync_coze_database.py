"""创建并填充新的 Coze 演示订单表；保留原表，不更新或删除原记录。

默认只生成请求文件。显式 --apply 才执行已授权的数据库同步。
令牌仅从环境或本地 .env.local 读取，不能写入交付包。
"""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from prepare_coze import prepare, FIELDS

ROOT=Path(__file__).resolve().parents[1]
API='https://api.coze.cn'


def load_settings():
    config={}
    local=ROOT/'.env.local'
    if local.exists():
        for line in local.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value=line.split('=',1)
                if key.strip() in ['COZE_API_TOKEN','COZE_WORKSPACE_ID','COZE_APP_ID']:
                    config[key.strip()]=value.strip().strip('\"').strip("'")
    for key in ['COZE_API_TOKEN','COZE_WORKSPACE_ID','COZE_APP_ID']:
        if os.getenv(key):config[key]=os.environ[key]
    return config


def call(method,path,token,body=None):
    payload=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None
    request=urllib.request.Request(API+path,data=payload,method=method,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=25) as response:
            result=json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # 不回显请求头或令牌。
        raise RuntimeError(f'Coze 返回 HTTP {exc.code}；同步未完成，请检查鉴权和权限。') from None
    except (urllib.error.URLError,TimeoutError,ValueError):
        raise RuntimeError('Coze 请求未成功；同步未完成，请检查连接。') from None
    if result.get('code') != 0:
        raise RuntimeError('Coze 返回业务错误 '+str(result.get('code'))+'；同步未完成。')
    return result.get('data',{})


def sync():
    schema,insert=prepare()
    config=load_settings()
    token=config.get('COZE_API_TOKEN','')
    workspace=config.get('COZE_WORKSPACE_ID','')
    app=config.get('COZE_APP_ID','')
    if not token or not workspace.isdigit() or (app and not app.isdigit()):
        raise RuntimeError('缺少有效本机令牌或工作空间 ID；请填写 .env.local。尚未执行网络写入。')
    schema['workspace_id']=workspace
    if app:schema['app_id']=app
    matching=[]
    page=1
    while True:
        data=call('GET','/v1/databases?'+urllib.parse.urlencode({'workspace_id':workspace,'page_num':page,'page_size':100}),token)
        matching += [item for item in data.get('items',[]) if item.get('table_name')==schema['table_name'] and str(item.get('app_id') or '0')==(app or '0')]
        if not data.get('has_more'):break
        page+=1
    if len(matching)>1:
        raise RuntimeError('同名演示表不唯一，请先核对目标表；未写入记录。')
    if matching:
        table=matching[0]
        actual={field['name']:str(field.get('type','')).lower() for field in table.get('fields',[])}
        expected={name:kind for name,(kind,_,_) in FIELDS.items()}
        if any(actual.get(name)!=kind for name,kind in expected.items()):
            raise RuntimeError('同名表结构与新结构不一致；未修改已有表。')
        database_id=str(table['id'])
    else:
        created=call('POST','/v1/databases',token,schema)
        database_id=str(created.get('database_id',''))
        if not database_id.isdigit():raise RuntimeError('创建结果未包含有效数据库 ID；请在 Coze 中核对。')
    state_path=ROOT/'coze_sync_state.json'
    state={'database_id':database_id,'workspace_id':workspace,'app_id':app,'table_name':schema['table_name'],'status':'created_not_verified'}
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    existing={}
    page=1
    while True:
        data=call('POST',f'/v1/databases/{database_id}/records/query',token,{'connector_id':'1024','page_num':page,'page_size':1000,'is_async':False})
        for row in data.get('items',[]):
            key=str(row.get('order_id',''))
            if key in existing:raise RuntimeError('目标表存在重复商城订单号；停止写入，请核对。')
            existing[key]=row
        if not data.get('has_more'):break
        page+=1
    additions=[]
    for row in insert['insert_rows']:
        current=existing.get(row['order_id'])
        if current is None:
            additions.append(row)
        elif any(str(current.get(key,''))!=str(value) for key,value in row.items()):
            raise RuntimeError('目标表存在内容不同的同编号订单；未覆盖，请核对。')
    if additions:
        response=call('POST',f'/v1/databases/{database_id}/records',token,{**insert,'insert_rows':additions})
        if int(response.get('affected_rows',-1))!=len(additions):
            raise RuntimeError('新增记录数与预期不一致；请核对，未宣称同步成功。')
    verified=call('POST',f'/v1/databases/{database_id}/records/query',token,{'connector_id':'1024','page_size':1000,'filter':{'logic':'and','conditions':[{'left':'source','operation':'equal','right':'demo'}]},'is_async':False})
    found={str(row.get('order_id')) for row in verified.get('items',[])}
    expected={row['order_id'] for row in insert['insert_rows']}
    if not expected.issubset(found):raise RuntimeError('同步后的回读验证失败，请核对目标表。')
    state.update(status='records_verified',demo_order_count=len(expected),added_count=len(additions))
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Coze 新演示表记录已回读验证；数据库 ID：'+database_id)
    print('原订单表及其数据未修改。需要在工作流中绑定新表和真实字段 ID。')
    return state


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        if args.apply:sync()
        else:
            prepare()
            print('已准备请求文件；未执行网络写入。')
    except RuntimeError as exc:
        parser.exit(1,str(exc)+'\n')
