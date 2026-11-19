"""Load and validate all configuration files.

Secrets come only from environment variables (or a local .env file that is
git-ignored). Everything else lives in config/*.yaml|json so behaviour can be
changed without touching Python code.
"""
from __future__ import annotations

import json
import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("BF_CONFIG_DIR", ROOT / "config"))


# ---------------------------------------------------------------- settings.yaml
class AppCfg(BaseModel):
    name: str = "Black Friday Hunter"
    timezone: str = "Asia/Yerevan"
    black_friday: date
    cyber_monday: date


class EmailCfg(BaseModel):
    sender: str
    recipients: list[str]
    top_n: int = 10
    opportunities_n: int = 5
    per_category_n: int = 2
    waiting_n: int = 8
    alerts_n: int = 8
    quiet_hours: list[int] = Field(default_factory=list)
    # Send an email at most every N hours (data is still collected every run).
    email_every_hours: float = 1
    focus_categories: list[str] = Field(default_factory=list)
    focus_per_category: int = 5


class CollectionCfg(BaseModel):
    user_agent: str
    timeout_seconds: float = 20
    max_retries: int = 3
    per_host_delay_seconds: float = 2.0
    pepper_enrich_per_site: int = 20
    pepper_enrich_refresh_hours: int = 6


class FiltersCfg(BaseModel):
    min_price_usd: float = 15
    min_pepper_temperature: float = 0
    exclude_categories: list[str] = Field(default_factory=lambda: ["other"])
    offer_ttl_hours: int = 48
    stale_after_hours: int = 3


class HistoryCfg(BaseModel):
    unchanged_observation_every_hours: int = 6
    min_days_for_stats: int = 3
    retention_days: int = 400


class ScoringCfg(BaseModel):
    weights: dict[str, float]
    history_proxy_max_share: float = 0.5
    reference_credibility: dict[str, float]
    classes: dict[str, float]

    @model_validator(mode="after")
    def _weights_sum(self) -> "ScoringCfg":
        total = sum(self.weights.values())
        if abs(total - 100) > 0.01:
            raise ValueError(f"scoring.weights must add up to 100 (got {total})")
        return self


class PrioritiesCfg(BaseModel):
    labels: dict[int, str] = Field(default_factory=lambda: {1: "Top priority", 2: "Priority 2", 3: "Priority 3"})
    # Whole categories that belong to a priority tier (in addition to watchlist items).
    categories: dict[int, list[str]] = Field(default_factory=dict)
    per_item: int = 3
    per_category_tier: int = 8


class AlertsCfg(BaseModel):
    enabled: bool = True
    min_score: float = 80
    priority1_min_score: float = 65
    record_low_min_score: float = 70
    watchlist_target_hit: bool = True
    realert_drop_pct: float = 3
    max_per_message: int = 6


class TelegramCfg(BaseModel):
    enabled: bool = True          # only active when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set
    send_reports: bool = True     # short summary each time the email report is sent
    send_alerts: bool = True
    summary_n: int = 5


class DatabaseCfg(BaseModel):
    path: str = "data/deals.db"


class Settings(BaseModel):
    app: AppCfg
    email: EmailCfg
    collection: CollectionCfg
    filters: FiltersCfg
    history: HistoryCfg
    scoring: ScoringCfg
    database: DatabaseCfg
    priorities: PrioritiesCfg = Field(default_factory=PrioritiesCfg)
    alerts: AlertsCfg = Field(default_factory=AlertsCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)


# ---------------------------------------------------------------- stores.yaml
class FeedCfg(BaseModel):
    url: str
    category: str | None = None   # hint used when the title itself is not specific


class SourceCfg(BaseModel):
    id: str
    type: str
    enabled: bool = True
    region: str
    currency: str
    feed_url: str | None = None
    feed_urls: list[str] = Field(default_factory=list)
    feeds: list[FeedCfg] = Field(default_factory=list)
    enrich_budget: int | None = None
    site: str | None = None
    queries: list[str] = Field(default_factory=list)
    page_size: int = 50
    marketplace: str | None = None
    max_per_keyword: int = 10


def source_feeds(src: "SourceCfg") -> list[FeedCfg]:
    """All feeds of a source, in order, without duplicates."""
    out: list[FeedCfg] = []
    seen: set[str] = set()
    for f in ([FeedCfg(url=src.feed_url)] if src.feed_url else []) + [FeedCfg(url=u) for u in src.feed_urls] + src.feeds:
        if f.url not in seen:
            seen.add(f.url)
            out.append(f)
    return out


class RetailerCfg(BaseModel):
    reliability: float
    aliases: list[str] = Field(default_factory=list)


class StoresCfg(BaseModel):
    sources: list[SourceCfg]
    default_reliability: float = 0.55
    retailers: dict[str, RetailerCfg]


# ---------------------------------------------------------------- categories.yaml
class CategoryCfg(BaseModel):
    label: str
    keywords: list[str]
    feed_categories: list[str] = Field(default_factory=list)
    est_weight_kg: float | None = None
    bf_extra_drop_pct: float = 0
    cm_extra_drop_pct: float = 0


class CategoriesCfg(BaseModel):
    categories: dict[str, CategoryCfg]
    non_product_keywords: list[str] = Field(default_factory=list)
    non_product_feed_categories: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- watchlist.json
class WatchItem(BaseModel):
    name: str
    priority: int = 2
    keywords: list[str]
    exclude: list[str] = Field(default_factory=list)
    target_price_usd: float | None = None
    target_delivered_usd: float | None = None
    urls: list[str] = Field(default_factory=list)

    @field_validator("keywords")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("watchlist item needs at least one keyword")
        return v


class Watchlist(BaseModel):
    watchlist: list[WatchItem] = Field(default_factory=list)


# ---------------------------------------------------------------- shipping_rates.yaml
class CustomsRegime(BaseModel):
    duty_free_value_eur: float
    duty_free_weight_kg: float
    rate_on_excess_pct: float
    min_eur_per_excess_kg: float = 0
    source: str = ""


class CustomsCfg(BaseModel):
    active_regime: str
    verified_on: str | None = None
    regimes: dict[str, CustomsRegime]
    include_international_shipping_in_value: bool = False

    @property
    def regime(self) -> CustomsRegime:
        return self.regimes[self.active_regime]


class ForwarderCfg(BaseModel):
    id: str
    name: str
    origin: str
    rate_per_kg: float | None = None
    rate_currency: str = "USD"
    min_chargeable_kg: float | None = None
    rounding_kg: float | None = None
    volumetric_divisor: float | None = None
    insurance_pct: float | None = None
    handling_fee: float = 0
    origin_sales_tax_pct: float | None = None
    verified: bool = False
    source: str = ""


class DirectShippingCfg(BaseModel):
    retailer: str
    origin: str
    flat_fee_usd: float | None = None
    per_kg_usd: float | None = None
    source: str = ""


class ExchangeCfg(BaseModel):
    primary: str = "cba"
    cba_url: str = "https://api.cba.am/exchangerates.asmx"
    fallback: str = "open_er_api"
    fallback_url: str = "https://open.er-api.com/v6/latest/USD"


class ShippingCfg(BaseModel):
    destination: dict[str, str]
    exchange_rates: ExchangeCfg
    customs: CustomsCfg
    forwarders: list[ForwarderCfg]
    domestic_shipping_default: dict[str, float | None] = Field(default_factory=dict)
    direct_shipping: list[DirectShippingCfg] | None = None


# ---------------------------------------------------------------- secrets
class Secrets(BaseModel):
    gmail_address: str | None = None
    gmail_app_password: str | None = None
    email_to: list[str] = Field(default_factory=list)
    bestbuy_api_key: str | None = None
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    exchange_rate_api_key: str | None = None
    optional_ai_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
        except ImportError:  # pragma: no cover
            pass

        def env(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        to = env("EMAIL_TO")
        return cls(
            gmail_address=env("GMAIL_ADDRESS"),
            # App passwords are shown with spaces by Google; SMTP accepts them without.
            gmail_app_password=(env("GMAIL_APP_PASSWORD") or "").replace(" ", "") or None,
            email_to=[x.strip() for x in to.split(",") if x.strip()] if to else [],
            bestbuy_api_key=env("BESTBUY_API_KEY"),
            ebay_client_id=env("EBAY_CLIENT_ID"),
            ebay_client_secret=env("EBAY_CLIENT_SECRET"),
            exchange_rate_api_key=env("EXCHANGE_RATE_API_KEY"),
            optional_ai_api_key=env("OPTIONAL_AI_API_KEY"),
            telegram_bot_token=env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=env("TELEGRAM_CHAT_ID"),
        )


class Config(BaseModel):
    settings: Settings
    stores: StoresCfg
    categories: CategoriesCfg
    watchlist: Watchlist
    shipping: ShippingCfg
    secrets: Secrets

    @property
    def db_path(self) -> Path:
        p = Path(os.environ.get("BF_DB_PATH", self.settings.database.path))
        return p if p.is_absolute() else ROOT / p


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        if path.suffix == ".json":
            return json.load(fh)
        return yaml.safe_load(fh)


def load_config(config_dir: Path | None = None, secrets: Secrets | None = None) -> Config:
    d = Path(config_dir or CONFIG_DIR)
    return Config(
        settings=Settings.model_validate(_read(d / "settings.yaml")),
        stores=StoresCfg.model_validate(_read(d / "stores.yaml")),
        categories=CategoriesCfg.model_validate(_read(d / "categories.yaml")),
        watchlist=Watchlist.model_validate(_read(d / "watchlist.json")),
        shipping=ShippingCfg.model_validate(_read(d / "shipping_rates.yaml")),
        secrets=secrets if secrets is not None else Secrets.from_env(),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
