"""资料库路由：列出 HTML 文档 / 研报，并原样返回文件供 iframe 展示"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.models.library import LibraryListResponse
from api.services.library_service import library_service

router = APIRouter()


@router.get("/", response_model=LibraryListResponse)
def list_library():
    return library_service.list_items()


@router.get("/file/{root}/{rel_path:path}")
def get_library_file(root: str, rel_path: str):
    resolved = library_service.resolve(root, rel_path)
    if not resolved:
        raise HTTPException(status_code=404, detail="文件不存在或不允许访问")
    path, media_type = resolved
    # no-cache：研报改完刷新就要看到新版；nosniff：类型由白名单决定，不让浏览器猜
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )
