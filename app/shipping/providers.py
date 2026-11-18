"""Forwarder / direct-shipping route lookup from shipping_rates.yaml."""
from __future__ import annotations

from app.config import DirectShippingCfg, ForwarderCfg, ShippingCfg
from app.utils.text import fold


def forwarders_for(region: str, cfg: ShippingCfg) -> list[ForwarderCfg]:
    return [f for f in cfg.forwarders if f.origin.upper() == region.upper()]


def direct_route_for(retailer: str, region: str, cfg: ShippingCfg) -> DirectShippingCfg | None:
    for d in cfg.direct_shipping or []:
        if fold(d.retailer) == fold(retailer) and d.origin.upper() == region.upper():
            return d
    return None
