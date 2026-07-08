from pydantic import BaseModel, Field
from typing import List, Optional

class FragmentMetadata(BaseModel):
    name: str
    provider: str
    size: int
    checksum: Optional[str] = None

class FileMetadata(BaseModel):
    filename: str
    total_size: int
    fragments: List[FragmentMetadata]
    redundancy_ratio: float = 1.5

class UploadResponse(BaseModel):
    file_id: str
    status: str
    fragments_count: int

class DownloadResponse(BaseModel):
    data: str # Base64 encoded or URL
