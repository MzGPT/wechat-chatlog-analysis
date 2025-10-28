from pydantic_settings import BaseSettings
from pydantic import AnyUrl, Field


class Settings(BaseSettings):
    # chatlog
    CHATLOG_HTTP_BASE: str = Field(default="http://127.0.0.1:5030")
    CHATLOG_DIR: str | None = None

    # n8n webhooks
    N8N_REPLY_WEBHOOK: str | None = None
    N8N_SUMMARY_WEBHOOK: str | None = None
    N8N_CONTACT_WEBHOOK: str | None = None
    N8N_SEND_WEBHOOK: str | None = None
    N8N_AUTH_TOKEN: str | None = None

    # API
    API_TOKEN: str | None = None

    # DB
    DATABASE_URL: str = Field(default="sqlite:///./data/app.db")

    # Server
    HOST: str = Field(default="127.0.0.1")
    PORT: int = Field(default=8000)
    SYNC_INTERVAL_SECONDS: int | None = Field(default=0)
    EMAIL_SYNC_INTERVAL_SECONDS: int | None = Field(default=0)

    # LLM
    SILICONFLOW_API_KEY: str | None = None
    SILICONFLOW_API_URL: str | None = "https://api.siliconflow.cn/v1"
    SILICONFLOW_MODEL: str | None = "Qwen/Qwen3-30B-A3B"
    SILICONFLOW_TOOL_MODEL: str | None = "Qwen/Qwen3-8B"

    # WeChatPadPro
    WECHATPAD_HTTP_BASE: str | None = None  # e.g., http://60.205.58.39:1238
    WECHATPAD_TEXT_PATH: str | None = "/api/v1/message/sendText"  # fallback path for text sending

    # Extensions / Adapters
    LANGBOT_ADAPTER_LOG_DIR: str | None = None  # e.g., ./data/adapters

    # Microsoft OAuth for Outlook/Hotmail
    MS_CLIENT_ID: str | None = None
    MS_TENANT: str | None = "consumers"  # common/organizations/consumers

    # NewsNow aggregation (server on :4445)
    NEWSNOW_ENABLED: bool = True
    NEWSNOW_API_BASE: str = Field(default="http://localhost:4445")
    NEWSNOW_CACHE_TTL: int = Field(default=300)  # seconds
    NEWSNOW_REFRESH_INTERVAL_SECONDS: int | None = Field(default=0)  # 0 = disabled (manual only)

    class Config:
        env_file = ".env"


settings = Settings()
