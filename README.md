# 零食电商智能客服演示项目

本仓库包含一个本地运行的客服演示系统：商品推荐和订单查询使用统一聊天入口。订单模块由 `snack_customer_service/` 提供，统一接入代码位于 `customer_service_demo/integration/`。

## 启动

需要 Python 3.10 或以上版本。进入统一客服目录后运行：

```powershell
cd customer_service_demo
python -m runtime.web --port 8787
```

浏览器打开 <http://127.0.0.1:8787/>。也可以运行 `customer_service_demo/start_demo.ps1`。

DeepSeek 模型配置是可选的。复制 `customer_service_demo/.env.example` 为本机 `.env` 并填写密钥；密钥文件不会提交到仓库。没有模型配置时，系统按项目中的本地兜底逻辑运行。

## 演示范围

- 商品、订单及物流记录是虚构演示数据。
- 物流使用本机模拟 HTTP 服务，不连接真实快递公司。
- 统一界面尚未接入真实商城身份、真实人工客服或退款服务。
- 当前业务验收状态和已知限制见 [`customer_service_demo/docs/10-订单模块整合与临期规则验收.md`](customer_service_demo/docs/10-订单模块整合与临期规则验收.md)。

独立订单客服原型的说明见 [`snack_customer_service/README.md`](snack_customer_service/README.md)。
