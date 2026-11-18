"""Collector interface and shared helpers."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from app.config import Config, SourceCfg
from app.database.repository import Repository
from app.models.schemas import CollectorStatus, RawOffer
from app.utils.http import PoliteClient
from app.utils.text import fold

log = logging.getLogger(__name__)


@dataclass
class CollectContext:
    cfg: Config
    http: PoliteClient
    repo: Repository | None
    now: datetime


@dataclass
class CollectResult:
    offers: list[RawOffer] = field(default_factory=list)
    status: CollectorStatus | None = None


class Collector(ABC):
    type: str = ""

    def __init__(self, source: SourceCfg):
        self.source = source

    @property
    def id(self) -> str:
        return self.source.id

    def available(self, ctx: CollectContext) -> tuple[bool, str | None]:
        """(can_run, reason_if_not). Override for API-key based collectors."""
        return True, None

    @abstractmethod
    def collect(self, ctx: CollectContext) -> tuple[list[RawOffer], int]:
        """Return (offers, skipped_count). May raise; the runner catches it."""

    def run(self, ctx: CollectContext) -> CollectResult:
        started = time.monotonic()
        ok, reason = self.available(ctx)
        if not ok:
            return CollectResult([], CollectorStatus(source=self.id, ok=True, note=f"skipped: {reason}"))
        try:
            offers, skipped = self.collect(ctx)
        except Exception as exc:  # one failing source must never stop the run
            log.exception("collector %s failed", self.id)
            return CollectResult([], CollectorStatus(source=self.id, ok=False, error=f"{type(exc).__name__}: {exc}"[:300],
                                                     duration_s=round(time.monotonic() - started, 1)))
        return CollectResult(offers, CollectorStatus(source=self.id, ok=True, offers=len(offers), skipped=skipped,
                                                     duration_s=round(time.monotonic() - started, 1)))


def build_offer(**kw) -> RawOffer | None:
    """Validate; return None (and log) instead of raising on bad data."""
    try:
        return RawOffer(**kw)
    except ValidationError as exc:
        log.debug("invalid offer from %s skipped: %s", kw.get("source"), exc.errors()[:2])
        return None


def normalize_retailer(name: str | None, cfg: Config) -> str:
    if not name:
        return "Unknown"
    n = fold(name).strip()
    n = n.removeprefix("www.")
    for canonical, r in cfg.stores.retailers.items():
        if n == fold(canonical) or n in (fold(a) for a in r.aliases):
            return canonical
    # host names like "www.aliexpress.com" / "amazon.de"
    for canonical, r in cfg.stores.retailers.items():
        if any(n.endswith(fold(a)) for a in r.aliases if "." in a):
            return canonical
    return name.strip()
