"""Map an offer to one of the configured categories, or mark it non-product."""
from __future__ import annotations

from app.config import CategoriesCfg
from app.utils.text import contains_phrase, fold

NON_PRODUCT = "non_product"
OTHER = "other"


def categorize(title: str, feed_category: str | None, cfg: CategoriesCfg) -> str:
    fc = fold(feed_category or "")
    if fc and any(fold(x) == fc for x in cfg.non_product_feed_categories):
        return NON_PRODUCT
    if any(contains_phrase(title, kw) for kw in cfg.non_product_keywords):
        return NON_PRODUCT
    # Title keywords are the most specific signal.
    for key, cat in cfg.categories.items():
        if any(contains_phrase(f" {title} ", kw.strip()) for kw in cat.keywords):
            return key
    # Fall back to the feed's own category name.
    if fc:
        for key, cat in cfg.categories.items():
            if any(fold(x) == fc for x in cat.feed_categories):
                return key
    return OTHER
