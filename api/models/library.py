"""资料库（HTML 文档 / 研报展示）模型"""

from pydantic import BaseModel, Field


class LibraryRootInfo(BaseModel):
    key: str
    label: str
    path: str
    exists: bool
    count: int = 0


class LibraryItem(BaseModel):
    id: str
    root: str
    root_label: str
    rel_path: str
    name: str
    title: str
    description: str = ""
    category: str = ""
    size: int = 0
    modified_at: str = ""
    url: str


class LibraryListResponse(BaseModel):
    roots: list[LibraryRootInfo] = Field(default_factory=list)
    items: list[LibraryItem] = Field(default_factory=list)
    total: int = 0
