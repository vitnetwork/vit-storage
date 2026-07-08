from abc import ABC, abstractmethod
from typing import Optional, List

class CloudProvider(ABC):
    account_id: str

    @abstractmethod
    async def upload_fragment(self, data: bytes, name: str) -> bool: ...

    @abstractmethod
    async def download_fragment(self, name: str) -> Optional[bytes]: ...

    @abstractmethod
    async def delete_fragment(self, name: str) -> bool: ...

    @abstractmethod
    async def list_fragments(self) -> List[str]: ...

    @abstractmethod
    async def get_quota(self) -> dict: ...

    @abstractmethod
    async def get_latency(self) -> float: ...
