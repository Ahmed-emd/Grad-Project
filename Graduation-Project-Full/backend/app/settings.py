from __future__ import annotations

from pydantic import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Listables"
    app_env: str = "dev"
    base_url: str = "http://localhost:8000"
    secret_key: str
    cookie_secure: bool = False

    database_url: str

    admin_email: str = "admin@admin"
    admin_password: str = "123456"

    stripe_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_success_url: str = "http://localhost:8000/order-confirmed.html?order_id={ORDER_ID}"
    stripe_cancel_url: str = "http://localhost:8000/checkout.html"
    contact_email_to: str = ""
    contact_email_from: str = ""
    contact_email_password: str = ""



settings = Settings()  # type: ignore[call-arg]

