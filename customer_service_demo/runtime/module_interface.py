"""通用模块边界。只定义接口；推荐和路由实现属于后续阶段。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol

ResultStatus = Literal["completed", "needs_clarification", "no_match", "unsupported"]


@dataclass(frozen=True)
class ModuleContext:
    conversation_id: str
    request_id: str
    turn_id: str
    base_revision: int
    message: str
    module_state: Mapping[str, Any]
    # 调用方传入深拷贝或只读视图，模块不得自行提交共享状态。
    history: tuple[Mapping[str, Any], ...]
    selected_reference: Mapping[str, str | None] | None
    now: datetime
    # 未来身份、会员等来源于服务端，不来源于用户文本。
    trusted_context: Mapping[str, Any]


@dataclass(frozen=True)
class ModuleResult:
    # 状态限定及内容结构通过 JSON Schema 验证。
    status: ResultStatus
    content: str
    cards: tuple[Mapping[str, Any], ...]
    clarification: Mapping[str, Any] | None
    next_module_state: Mapping[str, Any]
    # 单独记录，不传入默认客服消息。
    debug: Mapping[str, Any]


class CustomerServiceModule(Protocol):
    module_id: str

    async def handle(self, context: ModuleContext) -> ModuleResult:
        """返回候选结果；统一入口负责校验、版本核对与原子提交。"""
        ...
