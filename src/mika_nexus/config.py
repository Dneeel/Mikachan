from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: str
    owner_telegram_id: int
    allowed_telegram_ids: str = ""
    private_chats_only: bool = True
    bot_api_mode: Literal["public", "local"] = "public"
    local_bot_api_url: str | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    data_dir: Path = Path("data")
    max_active_downloads: int = 2
    max_active_per_user: int = 1
    max_links_per_message: int = 5
    search_page_size: int = 10
    search_session_ttl_seconds: int = 600
    confirmation_ttl_seconds: int = 900

    @field_validator("owner_telegram_id")
    @classmethod
    def positive_owner(cls, value: int) -> int:
        if not 0 < value < 2**63:
            raise ValueError("owner ID must be positive")
        return value

    @model_validator(mode="after")
    def check(self) -> "Settings":
        raw = self.allowed_telegram_ids.split(",") if self.allowed_telegram_ids else []
        if not raw or any(not x.strip().isdigit() or not 0 < int(x.strip()) < 2**63 for x in raw):
            raise ValueError("ALLOWED_TELEGRAM_IDS needs 1–4 positive numeric IDs")
        ids = {int(x.strip()) for x in raw}
        if len(ids) != len(raw) or len(ids) > 4 or self.owner_telegram_id not in ids:
            raise ValueError("allowlist must contain owner and at most four distinct users")
        if self.bot_token == "CHANGE_ME" or not self.bot_token:
            raise ValueError("configure BOT_TOKEN")
        if self.bot_api_mode == "local":
            if not (self.local_bot_api_url and self.telegram_api_id and self.telegram_api_hash):
                raise ValueError("local Bot API requires URL, API ID and API hash")
            parsed = urlsplit(self.local_bot_api_url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("local Bot API must bind to loopback HTTP")
        if not 1 <= self.max_active_downloads <= 2 or self.max_active_per_user != 1:
            raise ValueError("download concurrency must be at most two global and one per user")
        if not 1 <= self.max_links_per_message <= 5 or not 1 <= self.search_page_size <= 10:
            raise ValueError("invalid message/search limits")
        return self

    @property
    def allowlist(self) -> set[int]:
        return {int(x.strip()) for x in self.allowed_telegram_ids.split(",")}
