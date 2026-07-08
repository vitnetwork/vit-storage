import logging
from typing import List
from tachyon.providers.base import CloudProvider

logger = logging.getLogger(__name__)

class TachyonScheduler:
    """Orchestrates fragment distribution across available providers."""

    def __init__(self, providers: List[CloudProvider]):
        self.providers = providers
        logger.info(f"TachyonScheduler initialized with {len(providers)} providers")

    async def get_optimal_providers(self, fragment_count: int) -> List[CloudProvider]:
        """Returns providers sorted by latency and available quota."""
        # For now, simple round-robin or first-available
        if not self.providers:
            return []

        # Real logic would use provider.get_latency() and provider.get_quota()
        return (self.providers * (fragment_count // len(self.providers) + 1))[:fragment_count]
