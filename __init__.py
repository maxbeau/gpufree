"""
GPUFee.cn (算力自由) API Client
===============================
A comprehensive Python client for the gpufree.cn GPU cloud platform.

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

from gpufree.client import (
    GPUFeeClient,
    _b64url_decode,
    _extract_client_id,
    _yuan,
    BASE_URL,
    DEFAULT_TOKEN_FILE,
)

__all__ = [
    "GPUFeeClient",
    "_b64url_decode",
    "_extract_client_id",
    "_yuan",
    "BASE_URL",
    "DEFAULT_TOKEN_FILE",
]
