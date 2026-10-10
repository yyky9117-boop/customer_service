# 统一客服接入层

8787客服网页已切换到 `UnifiedChatService`，推荐、订单与分流澄清共用消息历史和会话版本，分别保留模块状态。客户端不能选择模块或传入订单归属身份。最新结果见 [订单整合验收](../docs/10-订单模块整合与临期规则验收.md)。

`router.py` 按明确咨询、号码和当前状态分流；混合问题及跨模块模糊指代先澄清。当前为规则分流，不宣称理解任意自然语言。推荐模型只接收推荐历史，订单模块只接收订单历史。

`order_adapter.py` 支持订单号、运单号、手机号、商品名称、候选序号与已选订单追问。新错误号码不回退到旧订单，多个明确编号先问查询哪一笔。提交前再次核对金额、状态、商品快照和归属。

`legacy_loader.py` 在独立命名空间复用原订单源码及检索→话术生成→规则校验→独立质检→明确兜底链路，共用主体DeepSeek配置及用量统计。原订单项目代码与SQLite库由适配层只读使用。当前界面不提供人工服务，适配层拒绝“点击联系人工”等不存在的入口。

网页正式启用两个事先约定的虚构账号：新客 `U_NEW` 对应账号A `demo_user_a`，老客 `U_RETURN` 对应账号B `demo_user_b`。其他身份不能查询订单。首次切换前自动备份旧网页会话库至 `.local/unified-backups/`，依据原网页服务端身份补充绑定，旧消息、状态、缓存保持原样；后续启动不重新启用显式禁用的绑定。

网页启动时在独立随机端口开启本机模拟快递HTTP服务，模拟库位于主体 `.local/unified-mock-courier.sqlite3`；接口失败清空旧轨迹和预计时间，不宣称查询到了实时商城物流。8765独立订单服务不受影响。原模拟人工工单功能未接入。原订单SKU与推荐SKU不同，不能据名字猜测它是哪张推荐卡片。

本项目当前依赖相邻 `../snack_customer_service` 的源码、话术文件与 `database/snack_orders_demo.sqlite`，不能单独复制主体后声称完整可独立运行。修改原订单Python实现后需重启统一服务。扩展协议可读副本位于 `integration/schemas/`，运行时仍从主体协议派生。

**以下内容为初步适配阶段的历史记录，包括当时禁用的绑定、尚未启用网页入口和历史测试数量。当前状态以上述说明和最新验收为准。**

## 本轮已实现

- `registry.py`：显式注册模块和事实核验器；默认模块固定为 `product_recommendation`，订单模块名为 `order_query`。
- `service.py`：共享会话编号、版本、消息历史和重复请求缓存，每轮只提交被选中模块的状态。推荐模块、计价及事实核验直接调用主体当前实现。可通过 `reply_generator` 注入主体正在优化的回复生成流程，该流程只用于推荐。
- `contracts.py`：在加载时复制主体当前协议，扩展订单状态与无卡片的订单文本回复。商品推荐仍必须通过原协议和原事实校验；不修改主体 `schemas/` 或协议生成器。
- `order_adapter.py`：按独立模块名加载原订单的 `normalize/query/render` 纯节点。订单库使用 SQLite 只读连接，不导入旧网页入口，不创建旧订单会话，不读取旧模型配置。
- `identity.py`：订单身份必须由服务端可信身份映射取得，手机号和消息文本不能更改订单归属。访客、未绑定和禁用的身份均拒绝查询。
- `store.py`：与主体相同的存储布局，并增加模块身份绑定和会话身份固定。跨模块原子提交使用扩展协议；当前只在独立验证数据库中使用。

## 数据库准备

已对 `.local/web-sessions.sqlite3` 添加 `module_identity_bindings`，准备以下**虚构演示映射**：

| 推荐主体身份 | 原订单身份 | 初始状态 |
|---|---|---|
| `U_NEW` | `demo_user_a` | 禁用 |
| `U_RETURN` | `demo_user_b` | 禁用 |

其他身份没有自动绑定订单。未把全部身份映射到同一个账号。该表不代表真实商城登录或授权。

准备脚本使用 SQLite 备份和事务，对迁移前后的 `sessions/requests/web_contexts` 全部记录进行摘要与数量比较。本轮记录证实这些数据保持一致。旧订单数据库没有执行迁移或写入。

重复执行准备脚本不会覆盖已有身份绑定或已有启用状态；遇到不同映射会停止并回滚：

```powershell
python tools/prepare_unification.py
```

备份位于 `.local/unification-backups/`，实际记录见 `acceptance/unification-migration.json`。准备脚本只添加表和禁用映射，不重启推荐服务。

**不要直接把 `UnifiedChatService` 指向当前推荐网页的会话数据库。** 当前原网页的协议不认识订单状态；初步验证必须使用独立库，避免产生原网页无法读取的会话。后续正式切换时再统一入口并迁移历史。

## 验证方法

在本项目根目录执行：

```powershell
python tools/verify_unification.py
```

该工具执行当前全部自动检查，并使用临时验证数据库跑通：

1. 当前推荐模块生成商品方案。
2. 同一会话显式分发给订单适配器，读取现有演示订单 `SN-DEMO-0001`。
3. 返回推荐模块，保留推荐条件和已选订单。

需求抽取使用明确标注的脚本输出，不访问 DeepSeek，不消耗模型额度。这是协议、推荐计算、数据库和会话边界验证，不能用来宣称自然语言理解准确率。

记录见 `acceptance/unification-execution.json`，详细测试日志在 `.local/unification-tests.log`。本轮执行了 202 项自动检查，其中 24 项为新增统一接入检查，均通过。跨模块读取实际演示订单库的检查也通过。

扩展后的可读 JSON 协议导出到 `integration/schemas/`；这组导出文件供评审使用。运行时由当前主体协议派生，协议变化后可重新执行验证工具刷新导出，不能把导出文件反写覆盖主体协议。

## 后续开发连接方式

统一入口接收主体现有的推荐模块实例，而不是新建另一套推荐算法：

```python
from integration.identity import IdentityBridge
from integration.order_adapter import LegacyOrderRepository, OrderQueryAdapter
from integration.service import UnifiedChatService
from integration.store import UnifiedSessionStore

# recommendation、clock、reply_generator 来自主体组装代码。
store = UnifiedSessionStore(".local/unified-sandbox.sqlite3")
service = UnifiedChatService(recommendation, store, clock=clock,
                             reply_generator=reply_generator)
identities = IdentityBridge(store)

# 仅在这个独立演示库中显式启用测试身份。
identities.bind("U_NEW", "demo_user_a", enabled=True)
adapter = OrderQueryAdapter(legacy_project_root,
                           LegacyOrderRepository(legacy_order_db), identities)
service.modules.register(adapter, adapter.verify)

# target_module 由服务端决定，不添加为客户端可随意传入的身份或路由字段。
response = await service.handle(request, trusted_context,
                                target_module="order_query")
```

请求结构沿用主体原 `chat-request`。默认不传 `target_module` 时仍执行商品推荐。订单目前支持明确的演示商城订单号、演示运单号、手机号，以及已选订单的后续发货查询；没有声称完成任意自然语言订单理解。推荐卡片引用不能用于选择订单。

订单结果目前仅有文本，保持现有前端可读消息形态，不注册订单卡片。号码优先级、账号过滤、发货状态回复复用原订单节点。历史物流轨迹主动清空并标注未查询；不会把数据库中的旧轨迹作为本轮接口调用结果。

## 本阶段范围与下一步

本轮只新增 `integration/`、两个准备/验证工具、统一接入测试和验收记录。没有修改推荐核心、主体会话入口、模型提示词、商品与营销资料、网页或密钥，也没有修改旧订单项目代码。主体优化期间发现原代码持续更新，因此采用隔离接入层，避免共同编辑核心文件。

待推荐优化稳定后再完成：

1. 正式替换网页统一入口，并根据最新主体实现收敛接入层中的会话编排。
2. 自然语言意图分流、混合问题处理和跨模块指代。
3. 订单/物流卡片及其稳定引用协议。
4. 将原快递 HTTP 客户端接入统一会话，保存每次调用证据。
5. 将原人工 HTTP 客户端接入统一会话，携带推荐与订单上下文，保证工单重试幂等。
6. 统一 SKU 与订单成交快照，扩充模拟订单，明确业务演示时间口径。
7. 完整浏览器及真实模型验收。

原订单项目的物流和人工演示接口继续存在，但本次统一接入验证没有调用它们。当前没有真实交易、真实商城身份或真实人工工作人员接入。
