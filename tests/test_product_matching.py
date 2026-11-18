from app.analysis.product_matching import (
    clean_title,
    extract_specs,
    gtin_normalize,
    product_id_for,
    same_product,
)


def same(a, b):
    return same_product(extract_specs(a), extract_specs(b)).same


def test_clean_title_removes_noise():
    assert clean_title("102° - Samsung 990 PRO 2TB (Prime)") == "Samsung 990 PRO 2TB"
    assert clean_title("Fender Strat for $744 + free shipping") == "Fender Strat"
    assert "HonTech" not in clean_title("HONOR MagicPad 2 12.3\" Tablet from HonTech Store")


def test_extract_specs_gpu_cpu_capacity():
    s = extract_specs("MSI GeForce RTX 5080 16G Gaming Trio OC")
    assert s.brand == "msi" and s.gpu == "rtx 5080"
    s = extract_specs("AMD Ryzen 7 9800X3D Prozessor")
    assert s.cpu == "ryzen 7 9800x3d"
    s = extract_specs("Samsung 990 PRO NVMe SSD 2 TB")
    assert s.capacities == ("2tb",) and "pro" in s.variants
    s = extract_specs("Apple MacBook Pro 14 M5 16GB 512GB")
    assert s.chip == "m5" and s.capacities == ("16gb", "512gb")


def test_same_product_across_shops_and_languages():
    assert same("Samsung 990 PRO 2TB NVMe M.2 SSD", "Samsung 990 PRO SSD 2TB, PCIe 4.0 M.2 2280, bis zu 7.450 MB/s")
    assert same("Nintendo Switch 2 Gaming Console", "Nintendo Switch 2 Console")
    assert same("Vizio Quantum Series VQD50X-10 50\" 4K HDR Smart TV", "Vizio Quantum VQD50X-10 50\" 4K UHD TV")


def test_different_configuration_is_not_merged():
    assert not same("Samsung 990 PRO 2TB NVMe SSD", "Samsung 990 PRO 1TB NVMe SSD")       # capacity
    assert not same("Samsung 990 PRO 2TB", "Samsung 990 EVO 2TB")                           # model
    assert not same("Apple iPhone 17 Pro 256GB", "Apple iPhone 17 Pro Max 256GB")           # variant
    assert not same("Apple iPhone 17 Pro 256GB", "Apple iPhone 17 Pro 256GB (Renewed)")      # condition
    assert not same("Gigabyte RTX 5080 Windforce", "Gigabyte RTX 5070 Ti Windforce")         # gpu
    assert not same("LG OLED55C5 55\" OLED TV", "LG OLED65C5 65\" OLED TV")                   # size/code
    assert not same("The Legend of Zelda: Ocarina of Time for Nintendo Switch 2", "Nintendo Switch 2 Console")
    assert not same("Nike Men's Kobe Air Force 1 Low Shoes", "Nike Men's Air Force 1 '07 Shoes")


def test_weak_titles_get_distinct_ids():
    """Regression: sparse specs must never make unrelated products share an id."""
    titles = ["CAT Men's Second Shift Steel Toe 6\" Work Boots", "Xbox Wireless Controller – Forza Horizon",
              "Augusta Dining Table & 6 Chairs", "Wrangler Hardside Spinner 22\" Carry-On Luggage",
              "French Connection 22\" Expandable Hardside Spinner"]
    ids = {product_id_for(extract_specs(t)) for t in titles}
    assert len(ids) == len(titles)


def test_gtin():
    assert gtin_normalize("0887276-711234") == "887276711234"
    assert gtin_normalize("123") is None
    a = product_id_for(extract_specs("x"), "0887276711234")
    b = product_id_for(extract_specs("totally different title"), "887276711234")
    assert a == b


def test_known_listing_is_rematched_when_rules_improve(cfg, now):
    """A listing wrongly attached to a product in the past gets re-matched."""
    from app.database.models import Product
    from app.database.repository import Repository, make_engine, make_session_factory
    from app.models.schemas import RawOffer

    eng = make_engine(cfg.db_path)
    with make_session_factory(eng)() as s:
        repo = Repository(s)
        o1 = RawOffer(source="t", source_item_id="1", title="Nike Air Max Torch 4 Men's Shoes", url="https://x.y/1",
                      retailer="Nike", region="US", currency="USD", price=50, observed_at=now)
        p1, _, _ = repo.find_or_create_product(o1, "clothing", now)
        repo.upsert_listing(p1, o1, now, 50, 6)
        # simulate an old wrong merge: listing 2 attached to product 1
        o2 = o1.model_copy(update={"source_item_id": "2", "title": "Nike Men's Air Max Bia Shoes"})
        repo.upsert_listing(p1, o2, now, 50, 6)
        s.commit()
        p2, _, how = repo.find_or_create_product(o2, "clothing", now)
        assert p2.id != p1.id and how != "known listing"
        assert s.get(Product, p1.id) is not None
