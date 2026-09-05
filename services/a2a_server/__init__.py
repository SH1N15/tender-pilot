"""A2A 服务（官方 a2a-sdk 1.1.2）。"""

from services.a2a_server.app import A2A_SDK_VERSION, A2A_SPEC_REF, build_agent_card, create_a2a_app
from services.a2a_server.executor import BidMasterAgentExecutor

__all__ = ["build_agent_card", "create_a2a_app", "BidMasterAgentExecutor", "A2A_SDK_VERSION", "A2A_SPEC_REF"]
