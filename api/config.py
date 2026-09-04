"""API 配置 — 从环境变量读取"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """API 配置，优先从环境变量 / .env 读取"""

    # 数据库
    db_path: str = "data/stock_data.db"
    data_mode: str = "websearch"

    # CORS
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # API
    api_port: int = 8000
    api_prefix: str = "/api/v1"

    # 看板「资料库」页展示的 HTML 目录：`路径=标签`，分号分隔，相对路径相对仓库根。
    # docs/ 入库；reports/ 被 .gitignore 忽略，适合放不想公开的个人研报。
    library_dirs: str = "docs=方案文档;reports=研报"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
