from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库
    database_url: str = "sqlite:///./supplier.db"

    # 单用户鉴权
    auth_username: str = "admin"
    auth_password: str = "please-change-me"
    jwt_secret: str = "dev-secret-change-in-prod"
    jwt_expires_hours: int = 24

    # Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-4-5-20251001"

    # 对象存储（S3 兼容）
    storage_provider: str = "local"  # local | r2 | s3 | oss
    storage_bucket: str = ""
    storage_region: str = "auto"
    storage_endpoint: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_public_base_url: str = ""
    local_storage_dir: str = "./uploads"  # storage_provider=local 时用

    # 汇率源
    fx_api_base: str = "https://api.frankfurter.app"

    # CORS
    frontend_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
