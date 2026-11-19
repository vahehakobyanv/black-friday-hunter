"""Estimated total cost of a deal delivered to Yerevan.

Every line says whether it is known or estimated. Unknown values are NOT
guessed: the line shows "unavailable" and the total is marked incomplete.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.config import Config, ForwarderCfg
from app.models.schemas import CostLine, DeliveryEstimate, PriceKind
from app.shipping.currency import FxTable
from app.shipping.providers import direct_route_for, forwarders_for


@dataclass
class ShipInput:
    price: float
    currency: str
    region: str
    retailer: str
    category: str
    free_shipping: bool | None = None
    shipping_cost: float | None = None       # domestic shipping in offer currency
    weight_kg: float | None = None
    dimensions_cm: tuple[float, float, float] | None = None
    price_kind: PriceKind = PriceKind.ADVERTISED


def chargeable_weight(actual_kg: float, fw: ForwarderCfg, dims: tuple[float, float, float] | None) -> tuple[float, str]:
    note = ""
    w = actual_kg
    if dims and fw.volumetric_divisor:
        vol = dims[0] * dims[1] * dims[2] / fw.volumetric_divisor
        if vol > w:
            w, note = vol, f"volumetric {vol:.2f} kg"
    elif fw.volumetric_divisor is None:
        note = "volumetric weight not checked (no divisor/dimensions)"
    if fw.min_chargeable_kg:
        w = max(w, fw.min_chargeable_kg)
    if fw.rounding_kg:
        w = math.ceil(round(w / fw.rounding_kg, 6)) * fw.rounding_kg
    return round(w, 3), note


def customs_duty_eur(value_eur: float, weight_kg: float | None, cfg: Config) -> tuple[float, str]:
    r = cfg.shipping.customs.regime
    excess_value = max(0.0, value_eur - r.duty_free_value_eur)
    excess_weight = max(0.0, (weight_kg or 0) - r.duty_free_weight_kg)
    by_value = excess_value * r.rate_on_excess_pct / 100
    by_weight = excess_weight * r.min_eur_per_excess_kg
    if excess_value <= 0 and excess_weight <= 0:
        return 0.0, f"under €{r.duty_free_value_eur:.0f}/{r.duty_free_weight_kg:.0f} kg duty-free limit"
    duty = max(by_value, by_weight)
    return duty, (f"{r.rate_on_excess_pct:g}% of €{excess_value:.0f} above €{r.duty_free_value_eur:.0f}"
                  + (f", min €{r.min_eur_per_excess_kg:g}/kg over {r.duty_free_weight_kg:g} kg" if excess_weight else ""))


def estimate_delivery(inp: ShipInput, fx: FxTable, cfg: Config) -> DeliveryEstimate:
    lines: list[CostLine] = []
    warnings: list[str] = []
    complete = True
    price_usd = fx.to_usd(inp.price, inp.currency)
    if price_usd is None:
        return DeliveryEstimate(route="unavailable", lines=[], total_usd=None, total_amd=None, complete=False,
                                weight_kg=None, weight_is_estimate=False,
                                warnings=[f"no exchange rate for {inp.currency}"])
    lines.append(CostLine(label="Product price", amount_usd=round(price_usd, 2), kind=inp.price_kind))

    # weight
    weight, weight_est = inp.weight_kg, False
    if weight is None:
        est = cfg.categories.categories.get(inp.category)
        if est and est.est_weight_kg:
            weight, weight_est = est.est_weight_kg, True
            warnings.append(f"weight estimated ({weight:g} kg, category default)")

    # route
    direct = direct_route_for(inp.retailer, inp.region, cfg.shipping)
    fws = forwarders_for(inp.region, cfg.shipping)
    fw = next((f for f in fws if f.rate_per_kg is not None), fws[0] if fws else None)

    # domestic shipping to the forwarder warehouse
    domestic_usd: float | None
    if direct is not None:
        domestic_usd = 0.0
    elif inp.free_shipping:
        domestic_usd = 0.0
        lines.append(CostLine(label="Shipping to forwarder", amount_usd=0.0, kind=PriceKind.ADVERTISED, note="free shipping"))
    elif inp.shipping_cost is not None:
        domestic_usd = fx.to_usd(inp.shipping_cost, inp.currency)
        lines.append(CostLine(label="Shipping to forwarder", amount_usd=round(domestic_usd or 0, 2), kind=PriceKind.ADVERTISED))
    else:
        default = cfg.shipping.domestic_shipping_default.get(inp.region.upper())
        if default is not None:
            domestic_usd = fx.to_usd(default, inp.currency)
            lines.append(CostLine(label="Shipping to forwarder", amount_usd=round(domestic_usd or 0, 2),
                                  kind=PriceKind.ESTIMATED, note="configured default"))
        else:
            domestic_usd = None
            complete = False
            lines.append(CostLine(label="Shipping to forwarder", amount_usd=None, kind=PriceKind.UNVERIFIED,
                                  note="unknown — check at checkout"))

    # sales tax at origin
    tax_usd = 0.0
    if fw is not None and fw.origin_sales_tax_pct:
        tax_usd = price_usd * fw.origin_sales_tax_pct / 100
        lines.append(CostLine(label="Sales tax at origin", amount_usd=round(tax_usd, 2), kind=PriceKind.ESTIMATED))

    # international leg
    intl_usd: float | None = None
    route = "unavailable"
    if direct is not None:
        route = f"Direct from {inp.retailer}"
        if direct.flat_fee_usd is not None or direct.per_kg_usd is not None:
            if direct.per_kg_usd is not None and weight is None:
                complete = False
            else:
                intl_usd = (direct.flat_fee_usd or 0) + (direct.per_kg_usd or 0) * (weight or 0)
        else:
            complete = False
    elif fw is None:
        complete = False
        warnings.append(f"no forwarder configured for {inp.region}")
        route = f"{inp.region} → Yerevan (no forwarder configured)"
    else:
        route = f"{inp.region} shop → {fw.name} → Yerevan"
        if fw.rate_per_kg is None:
            complete = False
            warnings.append(f"{fw.name}: rate per kg not configured")
        elif weight is None:
            complete = False
            warnings.append("weight unknown")
        else:
            cw, note = chargeable_weight(weight, fw, inp.dimensions_cm)
            fee = cw * fw.rate_per_kg + (fw.handling_fee or 0)
            intl_usd = fx.convert(fee, fw.rate_currency, "USD")
            if intl_usd is None:
                complete = False
            label_note = f"{cw:g} kg × {fw.rate_per_kg:g} {fw.rate_currency}/kg" + (f"; {note}" if note else "")
            if not fw.verified:
                label_note += "; rate not verified"
            lines.append(CostLine(label="Forwarding to Yerevan", amount_usd=round(intl_usd, 2) if intl_usd is not None else None,
                                  kind=PriceKind.ESTIMATED, note=label_note))
    if intl_usd is None and not any(line.label == "Forwarding to Yerevan" for line in lines):
        lines.append(CostLine(label="Forwarding to Yerevan", amount_usd=None, kind=PriceKind.UNVERIFIED, note="unavailable"))

    # insurance (optional; shown only when configured)
    ins_usd = 0.0
    if fw is not None and fw.insurance_pct:
        ins_usd = price_usd * fw.insurance_pct / 100
        lines.append(CostLine(label="Insurance", amount_usd=round(ins_usd, 2), kind=PriceKind.ESTIMATED))
    elif fw is not None:
        warnings.append("insurance not included (rate not configured)")

    # customs
    value_usd = price_usd + (domestic_usd or 0) + tax_usd
    if cfg.shipping.customs.include_international_shipping_in_value:
        value_usd += intl_usd or 0
    value_eur = fx.convert(value_usd, "USD", "EUR")
    duty_eur_val: float | None = None
    if value_eur is None:
        complete = False
        customs_usd = None
        lines.append(CostLine(label="Armenian customs", amount_usd=None, kind=PriceKind.UNVERIFIED, note="no EUR rate"))
    else:
        duty_eur, cnote = customs_duty_eur(value_eur, weight, cfg)
        duty_eur_val = duty_eur
        customs_usd = fx.convert(duty_eur, "EUR", "USD") or 0.0
        if weight_est and weight and weight > cfg.shipping.customs.regime.duty_free_weight_kg:
            cnote += " (weight estimated)"
        lines.append(CostLine(label="Armenian customs", amount_usd=round(customs_usd, 2), kind=PriceKind.ESTIMATED, note=cnote))

    total = price_usd + (domestic_usd or 0) + tax_usd + (intl_usd or 0) + ins_usd + (customs_usd or 0)
    return DeliveryEstimate(
        route=route,
        lines=lines,
        total_usd=round(total, 2),
        total_amd=round(fx.to_amd(total, "USD") or 0) if fx.to_amd(total, "USD") else None,
        complete=complete,
        weight_kg=weight,
        weight_is_estimate=weight_est,
        warnings=warnings,
        customs_value_eur=round(value_eur, 2) if value_eur is not None else None,
        duty_eur=round(duty_eur_val, 2) if duty_eur_val is not None else None,
        duty_free_limit_eur=cfg.shipping.customs.regime.duty_free_value_eur,
    )
