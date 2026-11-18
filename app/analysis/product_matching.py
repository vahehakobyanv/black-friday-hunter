"""Identify the same product (model + configuration) across shops and feeds.

Strategy, strongest first:
1. Same GTIN/UPC/EAN  -> same product.
2. Otherwise compare extracted specs. Two offers are the SAME product only if
   brand, condition, variant words (Pro/Max/Ultra/Ti ...), storage/memory
   capacities, GPU/CPU/chip ids and screen size all agree, their model-number
   tokens overlap without conflicting codes, and the cleaned titles are similar.
3. When in doubt they stay SEPARATE. A wrong merge would corrupt price history,
   a missed merge only shows the product twice.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.utils.text import fold

KNOWN_BRANDS = [
    "apple", "samsung", "lenovo", "dell", "hp", "asus", "acer", "msi", "gigabyte", "razer", "microsoft",
    "google", "sony", "lg", "tcl", "hisense", "philips", "panasonic", "xiaomi", "oneplus", "nothing",
    "huawei", "honor", "oppo", "motorola", "nvidia", "amd", "intel", "corsair", "kingston", "crucial",
    "western digital", "wd", "seagate", "sandisk", "teamgroup", "g.skill", "be quiet!", "seasonic",
    "nzxt", "cooler master", "noctua", "zotac", "palit", "pny", "sapphire", "powercolor", "xfx", "asrock",
    "nintendo", "valve", "meta", "bose", "jbl", "sennheiser", "beats", "anker", "ugreen", "logitech",
    "steelseries", "hyperx", "dyson", "bosch", "siemens", "miele", "delonghi", "de'longhi", "philips",
    "roborock", "ecovacs", "irobot", "nike", "adidas", "puma", "new balance", "asics", "the north face",
    "levi's", "garmin", "fitbit", "casio", "seiko", "samsonite", "amazon", "tp-link", "netgear", "avm",
    "ring", "eufy", "tado", "ninja", "tefal", "braun", "oral-b", "lego", "dji", "gopro", "canon", "nikon",
    "fujifilm", "benq", "aoc", "viewsonic", "iiyama", "kindle", "thermos", "stanley", "victorinox",
]
_BRAND_ALIASES = {"wd": "western digital", "de'longhi": "delonghi", "avm": "avm", "kindle": "amazon"}

VARIANT_WORDS = {"pro", "max", "ultra", "plus", "mini", "lite", "air", "fe", "se", "xl", "super", "ti",
                 "xt", "xtx", "gre", "slim", "oled", "fold", "flip", "edge", "neo"}
REFURB_WORDS = ["refurbished", "renewed", "generalüberholt", "generaluberholt", "b-ware", "used", "gebraucht",
                "pre-owned", "open box", "open-box", "certified refurbished", "wie neu", "like new"]
STOPWORDS = {
    "the", "and", "with", "for", "from", "in", "of", "a", "an", "to", "at", "on", "by", "new", "deal",
    "black", "friday", "cyber", "monday", "sale", "offer", "price", "best", "lowest", "free", "shipping",
    "delivery", "prime", "code", "w", "inc", "incl", "only", "bundle", "edition", "version", "und", "mit",
    "fur", "für", "von", "bei", "der", "die", "das", "im", "zum", "zur", "versand", "kostenlos", "gratis",
    "angebot", "bestpreis", "tiefstpreis", "neu", "stück", "stuck", "set", "pack", "x", "uk", "eu", "de",
}
_UNIT_TOKEN = re.compile(r"^\d+([.,]\d+)?(hz|mhz|ghz|w|mah|wh|mb/?s|gb/?s|k|p|cm|mm|m|kg|g|ml|l|v|db|fps|gen|nm|x|%|st|th|nd|rd|ms|s)$")
_CODE_TOKEN = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9-]{4,}$")


@dataclass
class ProductSpecs:
    brand: str | None = None
    condition: str = "new"
    capacities: tuple[str, ...] = ()
    gpu: str | None = None
    cpu: str | None = None
    chip: str | None = None
    screen_in: str | None = None
    variants: frozenset[str] = frozenset()
    model_tokens: frozenset[str] = frozenset()
    code_tokens: frozenset[str] = frozenset()
    clean_title: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "brand": self.brand,
            "condition": self.condition,
            "capacities": list(self.capacities),
            "gpu": self.gpu,
            "cpu": self.cpu,
            "chip": self.chip,
            "screen_in": self.screen_in,
            "variants": sorted(self.variants),
            "model_tokens": sorted(self.model_tokens),
        }

    @property
    def model_label(self) -> str:
        parts = [self.gpu, self.cpu, self.chip, " ".join(sorted(self.model_tokens)) or None,
                 " ".join(sorted(self.variants)) or None, "/".join(self.capacities) or None,
                 f'{self.screen_in}"' if self.screen_in else None]
        return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- cleaning
_NOISE_PATTERNS = [
    r"^\s*-?\d+(?:[.,]\d+)?\s*°\s*-?\s*",                 # Pepper temperature prefix "102° - "
    r"\bfor \$[\d,.]+.*$",                                  # dealnews "... for $744 + free shipping"
    r"\bfrom \$[\d,.]+.*$",
    r"\b(?:sold|dispatched|shipped) by\b.*$",
    r"\bfrom [\w&' -]+ store\b.*$",
    r"\bw/\s?code\b.*$", r"\bwith code\b.*$", r"\bmit code\b.*$",
    r"\((?:prime|amazon|refurbished[^)]*|renewed[^)]*)\)",
    r"[@|]\s*[\w.&' -]+$",
    r"\bbei [\w.&' -]+$",
    r"\bfür \d+[.,]?\d*\s?€.*$", r"\bfur \d+[.,]?\d*\s?€.*$",
    r"\b\d+[.,]?\d*\s?(?:€|eur|£|\$)\s*(?:statt|instead of|was)?.*$",
    r"\[[^\]]*\]",
]


def clean_title(title: str) -> str:
    t = title.replace(" ", " ")
    for pat in _NOISE_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -–|,;:")


def _detect_brand(folded: str) -> str | None:
    padded = " " + re.sub(r"[^a-z0-9!.&' -]+", " ", folded) + " "
    best: tuple[int, str] | None = None
    for brand in KNOWN_BRANDS:
        idx = padded.find(" " + brand + " ")
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, brand)
    if best is None:
        return None
    brand = best[1]
    return _BRAND_ALIASES.get(brand, brand)


def extract_specs(title: str, brand_hint: str | None = None) -> ProductSpecs:
    cleaned = clean_title(title)
    f = fold(cleaned)
    specs = ProductSpecs(clean_title=cleaned)

    specs.brand = fold(brand_hint) if brand_hint else _detect_brand(f)
    if specs.brand is None:
        # Unknown brand: the first word of a product title is almost always the brand
        # ("Vizio Quantum ...", "Wrangler Hardside ..."). Only used for matching.
        first = re.match(r"\s*([A-Za-z][\w&'!.-]{1,30})", cleaned)
        if first and fold(first.group(1)) not in STOPWORDS and first.group(1)[0].isupper():
            specs.brand = fold(first.group(1)).strip(".")
    if specs.brand:
        specs.brand = _BRAND_ALIASES.get(specs.brand, specs.brand)

    if any(w in fold(title) for w in REFURB_WORDS):
        specs.condition = "refurbished"

    caps = []
    for num, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s?(tb|gb)\b", f):
        n = num.replace(",", ".")
        n = n.rstrip("0").rstrip(".") if "." in n else n
        caps.append(f"{n}{unit}")
    specs.capacities = tuple(sorted(set(caps)))

    m = re.search(r"\b(rtx|gtx)\s?-?(\d{4})\s?(ti super|ti|super)?\b", f)
    if m:
        specs.gpu = " ".join(x for x in (m.group(1), m.group(2), m.group(3)) if x)
    else:
        m = re.search(r"\b(?:radeon\s)?rx\s?-?(\d{4})\s?(xtx|xt|gre)?\b", f)
        if m:
            specs.gpu = " ".join(x for x in ("rx", m.group(1), m.group(2)) if x)
        else:
            m = re.search(r"\barc\s?(b\d{3}|a\d{3})\b", f)
            if m:
                specs.gpu = f"arc {m.group(1)}"

    m = re.search(r"\bryzen\s?([3579])\s?(?:pro\s)?(\d{3,5}(?:x3d|xt|x|g|u|hs|h)?)\b", f)
    if m:
        specs.cpu = f"ryzen {m.group(1)} {m.group(2)}"
    else:
        m = re.search(r"\bcore\s?ultra\s?([3579])\s?(\d{3}[a-z]{0,2})\b", f)
        if m:
            specs.cpu = f"core ultra {m.group(1)} {m.group(2)}"
        else:
            m = re.search(r"\b(i[3579])[-\s](\d{4,5}[a-z]{0,2})\b", f)
            if m:
                specs.cpu = f"{m.group(1)}-{m.group(2)}"

    m = re.search(r"\bm([1-5])(?!\.\d)(?:\s(pro|max|ultra))?\b", f)
    if m and ("apple" in f or "macbook" in f or "ipad" in f or "imac" in f or "mac mini" in f or specs.brand == "apple"):
        specs.chip = " ".join(x for x in ("m" + m.group(1), m.group(2)) if x)

    m = re.search(r"\b(\d{2}(?:[.,]\d)?)\s?(?:\"|”|''|zoll|inch|-inch|in\b|\s?\")", cleaned.lower())
    if m:
        specs.screen_in = m.group(1).replace(",", ".")

    tokens = re.findall(r"[a-z0-9][a-z0-9.+-]*", f)
    words = {t.strip(".-") for t in tokens}
    specs.variants = frozenset(w for w in words if w in VARIANT_WORDS)
    spec_strings = " ".join(filter(None, [specs.gpu, specs.cpu, specs.chip]))
    model_tokens = set()
    codes = set()
    for tok in words:
        if not tok or tok in STOPWORDS or not any(c.isdigit() for c in tok):
            continue
        if _UNIT_TOKEN.match(tok) or re.fullmatch(r"\d+(?:[.,]\d+)?(?:tb|gb)", tok):
            continue
        if tok in spec_strings.split():
            continue
        if re.fullmatch(r"(19|20)\d{2}", tok) and len(words) > 3:
            # a year is useful (MacBook 2024) but weak; keep it as model token
            pass
        model_tokens.add(tok)
        if _CODE_TOKEN.match(tok):
            codes.add(tok)
    specs.model_tokens = frozenset(model_tokens)
    specs.code_tokens = frozenset(codes)
    return specs


# --------------------------------------------------------------------------- identity
def _content_words(clean: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", fold(clean)) if w not in STOPWORDS and len(w) > 1}


def product_id_for(specs: ProductSpecs, gtin: str | None = None) -> str:
    """Deterministic id for a NEW product. Existing products are found via
    `same_product` first, so this is only used when nothing matches. The cleaned
    title words are always part of the key, so two different products can
    never collide just because their extracted specs are sparse."""
    if gtin:
        key = "gtin:" + re.sub(r"\D", "", gtin).lstrip("0")
    else:
        key = "|".join([
            specs.brand or "?", specs.condition, ",".join(specs.capacities), specs.gpu or "", specs.cpu or "",
            specs.chip or "", specs.screen_in or "", ",".join(sorted(specs.variants)),
            ",".join(sorted(specs.model_tokens)), " ".join(sorted(_content_words(specs.clean_title))),
        ])
    return hashlib.sha1(key.encode()).hexdigest()[:16]


@dataclass
class MatchResult:
    same: bool
    reason: str


def same_product(a: ProductSpecs, b: ProductSpecs) -> MatchResult:
    if not a.brand or not b.brand:
        return MatchResult(False, "brand unknown")
    if a.brand != b.brand:
        return MatchResult(False, "brand differs")
    if a.condition != b.condition:
        return MatchResult(False, "condition differs")
    if a.variants != b.variants:
        return MatchResult(False, "variant differs")
    if a.capacities != b.capacities:
        return MatchResult(False, "capacity differs")
    for attr in ("gpu", "cpu", "chip"):
        if getattr(a, attr) != getattr(b, attr):
            return MatchResult(False, f"{attr} differs")
    if a.screen_in and b.screen_in and a.screen_in != b.screen_in:
        return MatchResult(False, "screen size differs")
    if a.code_tokens and b.code_tokens and not (a.code_tokens & b.code_tokens):
        return MatchResult(False, "model codes conflict")
    ta, tb = fold(a.clean_title), fold(b.clean_title)
    wa, wb = _content_words(a.clean_title), _content_words(b.clean_title)
    jaccard = len(wa & wb) / len(wa | wb) if (wa | wb) else 0.0
    # An "anchor" is something that identifies a model: a chip/GPU/CPU id, a
    # shared alphanumeric model code, or a shared model number of >= 3 chars.
    anchored = bool(
        (a.gpu and a.gpu == b.gpu) or (a.cpu and a.cpu == b.cpu) or (a.chip and a.chip == b.chip)
        or (a.code_tokens & b.code_tokens)
        or any(len(t) >= 3 for t in (a.model_tokens & b.model_tokens))
    )
    if a.model_tokens and b.model_tokens and not (a.model_tokens & b.model_tokens) and not anchored:
        return MatchResult(False, "no shared model number")
    if anchored:
        ratio = fuzz.token_set_ratio(ta, tb)
        if ratio < 60 or jaccard < 0.25:
            return MatchResult(False, f"titles too different (set {ratio:.0f}, words {jaccard:.2f})")
        return MatchResult(True, f"model anchor + specs agree (set {ratio:.0f}, words {jaccard:.2f})")
    # No model anchor: demand identical model numbers and near-identical wording.
    if a.model_tokens != b.model_tokens:
        return MatchResult(False, "model numbers differ (no anchor)")
    ratio = fuzz.token_sort_ratio(ta, tb)
    if jaccard < 0.7 or ratio < 75:
        return MatchResult(False, f"no model anchor and titles differ (sort {ratio:.0f}, words {jaccard:.2f})")
    return MatchResult(True, f"no anchor but near-identical titles (sort {ratio:.0f}, words {jaccard:.2f})")


def gtin_normalize(gtin: str | None) -> str | None:
    if not gtin:
        return None
    digits = re.sub(r"\D", "", gtin)
    if len(digits) < 8:
        return None
    return digits.lstrip("0") or None
