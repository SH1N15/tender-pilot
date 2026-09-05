"""AG-UI 服务子包。"""

from services.agui.routes import router
from services.agui.service import AGUI_SDK_VERSION, AGUI_SPEC_REF

__all__ = ["router", "AGUI_SDK_VERSION", "AGUI_SPEC_REF"]
