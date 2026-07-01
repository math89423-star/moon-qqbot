"""图床提供者模块"""

from .provider_template import ProviderTemplate as ImageHostProvider
from .stardots_provider import StarDotsProvider

# CloudflareR2Provider 需要 boto3 (15MB, 非默认安装), 延迟导入。
# 使用方应直接从 cloudflare_r2_provider 导入，或在此 try/except:
#   from .cloudflare_r2_provider import CloudflareR2Provider
CloudflareR2Provider = None  # type: ignore[assignment]

def _load_cloudflare_r2_provider():
    """延迟加载 CloudflareR2Provider (仅在配置了 R2 图床时调用)。"""
    from .cloudflare_r2_provider import CloudflareR2Provider as _R2

    global CloudflareR2Provider
    CloudflareR2Provider = _R2
    return _R2


__all__ = ["CloudflareR2Provider", "ImageHostProvider", "StarDotsProvider"]
