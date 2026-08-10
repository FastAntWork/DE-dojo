"""Конфигурация приложения.

Единственное место, где читается окружение. Всё остальное получает готовый
`Settings` — так тесты подменяют конфиг явно, а не через monkeypatch os.environ.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки процесса.

    DSN задаётся целиком одной переменной, а не собирается из host/port/user:
    внутри контейнера хост `postgres`, снаружи `127.0.0.1`, и склейка по частям
    каждый раз рождает вопрос «а какой порт тут имеется в виду».
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: str = Field(default="local", alias="DOJO_ENV")
    log_level: str = Field(default="INFO", alias="DOJO_LOG_LEVEL")

    database_url: str = Field(
        default="postgresql://dojo:dojo_local_dev@127.0.0.1:5432/dojo",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        alias="REDIS_URL",
    )

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кешированный конфиг. Сбрасывается в тестах через `get_settings.cache_clear()`."""
    return Settings()
