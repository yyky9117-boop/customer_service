"""在独立命名空间加载订单源码，不更改原项目或全局导入路径。"""
import ast
import hashlib
import importlib.util
import sys
import threading
import types
import re
from pathlib import Path

_lock = threading.RLock()


def load(root, relative):
    root = Path(root).resolve()
    path = (root / (relative.replace(".", "/") + ".py")).resolve()
    if not path.is_relative_to(root):
        raise ValueError("订单模块路径越界")
    namespace = "_snack_unified_" + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    name = namespace + "." + relative
    with _lock:
        if name in sys.modules:
            return sys.modules[name]
        for count in range(1, len(name.split("."))):
            package_name = ".".join(name.split(".")[:count])
            if package_name not in sys.modules:
                package = types.ModuleType(package_name)
                package.__path__ = [str(root)]
                sys.modules[package_name] = package
        # 原订单模块使用顶层 model_adapter 导入；仅在内存中绑定到独立命名空间。
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "model_adapter" and node.level == 0:
                load(root, "model_adapter")
                node.module = namespace + ".model_adapter"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            exec(compile(tree, str(path), "exec"), module.__dict__)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module


class SharedOrderReplyModel:
    """订单生成与质检复用原提示词和生成器，共用主体配置及额度统计。"""
    def __init__(self, root, model):
        self.root, self.model = Path(root).resolve(), model
        self.failure = load(root, "model_adapter").ModelFailure

    @property
    def configured(self):
        return bool(getattr(self.model, "api_key", ""))

    def call(self, prompt, payload, repair=None):
        from runtime.errors import ServiceFailure
        system = (self.root / "order_reply/prompts" / (prompt + ".md")).read_text(encoding="utf-8")
        system += "\n当前统一客服界面不提供人工入口或人工服务。不能让用户点击联系人工、转接按钮或不存在的入口。可以说具体问题需要人工核实，但须说明本演示不提供此服务。没有执行退款、催单、通知仓库等动作。"
        try:
            return self.model._complete(system, payload, repair, 1400)
        except ServiceFailure as exc:
            raise self.failure("connection_failed" if exc.code == "data_unavailable" else "invalid_output") from None


def make_reply_generator(root, knowledge, model=None):
    """复用原链路，在适配边界核验当前界面的能力，不修改独立订单项目。"""
    base = load(root, "order_reply.generator").OrderReplyGenerator
    failure = load(root, "model_adapter").ModelFailure

    class UnifiedOrderReply(base):
        def packet(self, result, message, history):
            packet, retrieved = super().packet(result, message, history)
            packet["service_boundaries"] = {"human_entry_available": False, "refund_executed": False, "urge_shipping_executed": False}
            for row in retrieved:
                if '人工' in row['closing']:
                    row['closing'] = '具体问题需要进一步核实；本演示不提供人工服务。'
            for row in packet['knowledge']:
                if '人工' in row['closing_example']:
                    row['closing_example'] = '具体问题需要进一步核实；本演示不提供人工服务。'
            return packet, retrieved

        def render(self, output, packet):
            template = output.get('template', '') if isinstance(output, dict) else ''
            if isinstance(template, str) and re.search(r'点击.{0,18}人工|联系人工(?:按钮|入口)|通过.{0,18}人工入口', template):
                raise failure('unavailable_human_entry')
            return super().render(output, packet)

    return UnifiedOrderReply(knowledge, model=model, timeout_seconds=60)
