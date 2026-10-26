"""The shared kernel of the application layer (AUD-02 step 1, AUD-15).

The types and small ports that many features share live here once, instead of in whichever
feature module needed them first. The kernel imports only the standard library and the domain,
and never a feature module.
"""

from htco_application.kernel.errors import MAX_ERROR_LENGTH, JobError, describe_error
from htco_application.kernel.events import EVENTS_DESTINATION
from htco_application.kernel.locks import LockKeys
from htco_application.kernel.offload import Offload, run_inline
from htco_application.kernel.ports import ConnectedShops, ShopOwners
from htco_application.kernel.types import JsonValue, Page, TenantShop

__all__ = [
    "EVENTS_DESTINATION",
    "MAX_ERROR_LENGTH",
    "ConnectedShops",
    "JobError",
    "JsonValue",
    "LockKeys",
    "Offload",
    "Page",
    "ShopOwners",
    "TenantShop",
    "describe_error",
    "run_inline",
]
