"""GPUFee.cn (算力自由) API client.

Usage as library::

    from gpufree import GPUFeeClient

    client = GPUFeeClient()
    status = client.get_status()
    print(status["balance_summary"]["balance_yuan"])

Usage as CLI::

    gpufree status
    gpufree balance
    gpufree instances
"""

__version__ = "0.2.0"

from .client import (
    BASE_URL,
    DEFAULT_TOKEN_FILE,
    GPUFeeClient,
    TOKEN_REFRESH_LEEWAY,
)

__all__ = [
    "BASE_URL",
    "DEFAULT_TOKEN_FILE",
    "GPUFeeClient",
    "TOKEN_REFRESH_LEEWAY",
    "__version__",
]
