from __future__ import annotations

from app.collectors.base import Collector
from app.collectors.bestbuy import BestBuyCollector
from app.collectors.dealnews import DealnewsCollector
from app.collectors.ebay import EbayCollector
from app.collectors.generic_store import GenericStoreCollector
from app.collectors.pepper import PepperCollector
from app.config import Config

TYPES: dict[str, type[Collector]] = {
    "pepper": PepperCollector,
    "dealnews": DealnewsCollector,
    "bestbuy": BestBuyCollector,
    "ebay": EbayCollector,
    "generic_store": GenericStoreCollector,
}


def build_collectors(cfg: Config) -> list[Collector]:
    out = []
    for src in cfg.stores.sources:
        if not src.enabled:
            continue
        cls = TYPES.get(src.type)
        if cls is None:
            raise ValueError(f"unknown source type {src.type!r} in stores.yaml")
        out.append(cls(src))
    return out
