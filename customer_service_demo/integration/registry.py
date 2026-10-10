"""只允许服务端显式选择模块，默认沿用商品推荐。"""
from dataclasses import dataclass
from typing import Callable

from runtime.errors import ServiceFailure
from runtime.module_interface import CustomerServiceModule, ModuleContext, ModuleResult

MODULE_IDS = frozenset({"product_recommendation", "order_query", "conversation_router"})


@dataclass(frozen=True)
class ModuleEntry:
    module: CustomerServiceModule
    verify: Callable[[ModuleResult, ModuleContext], None]


class ModuleRegistry:
    def __init__(self, default_module, verify):
        if default_module.module_id != "product_recommendation":
            raise ValueError("统一入口必须以商品推荐为默认模块")
        self._entries = {}
        self.register(default_module, verify)

    def register(self, module, verify):
        if module.module_id not in MODULE_IDS or not callable(verify):
            raise ValueError("模块必须使用已定义协议并提供事实核验器")
        if module.module_id in self._entries:
            raise ValueError("不能隐式覆盖已注册模块")
        self._entries[module.module_id] = ModuleEntry(module, verify)

    def resolve(self, target=None):
        entry = self._entries.get(target or "product_recommendation")
        if entry is None:
            raise ServiceFailure("invalid_request", "该模块尚未启用，请使用当前已开放的客服功能。")
        return entry

    @property
    def module_ids(self):
        return tuple(self._entries)
