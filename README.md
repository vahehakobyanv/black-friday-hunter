# Black Friday Deal Hunter 2026

An deal-intelligence service for a shopper in **Yerevan, Armenia**. It collects
real offers from permitted deal feeds and official APIs, matches the same product across
shops, records its own price history, predicts Black Friday / Cyber Monday prices,
estimates the **delivered-to-Yerevan** cost, scores every deal from 0 to 100, and emails an
HTML report every 4 hours (no emails 00:00–08:00 Yerevan time; prices are still collected hourly).

- **Black Friday 2026:** 27 November · **Cyber Monday:** 30 November. After that it keeps tracking ongoing discounts.
- **Language:** email and Telegram are in Russian (product names stay as the shop writes them).
- **Region:** USA shops only (since 22 Nov 2026). The German/UK collectors stay in the code, disabled in `config/stores.yaml`.
- **Cost:** €0. GitHub Actions (public repo) + Gmail. No VPS, no paid database, no paid AI.

> Honesty rules built into the code: no invented prices, history, weights or shipping rates.
> Anything unknown is shown as **unavailable** or **estimated**, and every price is labelled
> CONFIRMED / ADVERTISED / ESTIMATED / PREDICTED / UNVERIFIED.

---

## How it works

```
GitHub Actions (cron :17 every hour, no overlap)
 │
 ├─ restore data/deals.db  ◄── branch `data` (deals.db.gz, single commit, force-pushed)
 │
 ├─ exchange rates ── Central Bank of Armenia (fallback: open.er-api.com, then cached)
 │
 ├─ collectors (each isolated: one failure never stops the run)
 │    mydealz.de RSS (DE) · hotukdeals RSS (UK) · dealnews RSS (US, 12 category feeds)
 │    Best Buy API* · eBay Browse API* · watchlist product pages (robots.txt permitting)
 │                                                   * only when you add a free API key
 ├─ normalise → categorise (13 categories) → match products across shops
 ├─ price history (own observations only) → 30/90/180-day stats, record lows, trend
 ├─ prediction (BF/CM) → delivery to Yerevan (forwarder + customs + FX) → score 0–100
 ├─ HTML report → Gmail SMTP (App Password)
 └─ save database ──► branch `data`  (+ 14-day backup artifact)
```

### Why these data sources
Amazon, Walmart, Newegg, MediaMarkt, Currys and most big shops have **no free API** and forbid
or block automated scraping (especially from cloud IPs). This project does **not** bypass
blocks or CAPTCHAs. Instead it reads public deal feeds that are allowed by `robots.txt` and
already cover those shops:

| Source | Region | What it gives | Covers |
|---|---|---|---|
| mydealz.de (Pepper) | DE | price, shop, community temperature, **next-best price** at another shop, expiry, shipping | Amazon.de, MediaMarkt, Saturn, notebooksbilliger, Alternate, Cyberport, Lenovo/Samsung DE … |
| hotukdeals (Pepper) | UK | same | Amazon.co.uk, Currys, Argos, John Lewis, Very, Scan … |
| dealnews | US | editor-verified price, retailer, "was" price, staff picks | Amazon, Walmart, Best Buy, Newegg, B&H, Adorama, Dell, HP, Lenovo, Samsung, Apple, Nike, Adidas … |
| dealnews store feeds | US | every deal editors found at Amazon, Walmart, Best Buy, Costco, Newegg, Woot, Lenovo, Dell, Samsung, Apple, Home Depot, eBay | those shops |
| DealCatcher RSS | US | one product per item with price (mostly Amazon, also Walmart, Home Depot, Macy's) | Amazon … |
| 9to5Toys RSS | US | editor-written deals; only single-product headlines with one clear price, "was" price from "($X off)" | Amazon, Best Buy … |
| Android Police / Tom's Guide / Wccftech RSS | US | editor-written deals, same strict single-product rule as 9to5Toys | Amazon, Best Buy … |
| BlackFriday.com ad pages | US | Black Friday / Cyber Monday ad scans: products and advertised prices (schema.org list) + link to the full ad in the email | Walmart, Target, Best Buy, Amazon, Costco, Newegg, Dell, Sam's Club, Home Depot, Kohl's, Macy's, Lowe's |
| Best Buy Products API | US | **confirmed** price, regular price, UPC, weight | Best Buy (free key) |
| eBay Browse API | US | **confirmed** prices for your watchlist keywords | eBay (free key) |
| Watchlist URLs | any | schema.org product data | only sites whose robots.txt allows it |

Checked and **not used**: Slickdeals, Reddit, Ben's Bargains, TechBargains, DealSea, Wirecutter, CheapAssGamer, Offers.com, Kinja Deals (block bots → 403/429); Android Authority (robots.txt forbids its feeds), Keepa (paid), Amazon
Creators API (needs an affiliate account with sales), Walmart/AliExpress affiliate APIs (approval).

### Scoring (config/settings.yaml → `scoring`)
| Points | Component | How |
|---|---|---|
| 30 | History | price vs. own 30/90-day average + record-low bonus. While own history is younger than 7 days, a **labelled proxy** (next-best-shop price + community votes) can earn at most 70 % of these points. |
| 20 | Discount | best credible discount; each reference is weighted: own history 1.0, next-best shop 0.9, retailer regular price 0.8, "list price" 0.6. List-price claims over 70 % off are ignored as inflated. |
| 20 | Prediction | little further drop expected → more points |
| 20 | Delivered cost | overhead of getting it to Yerevan (≤5 % = full, ≥50 % = 0). Incomplete estimates get 40 %. |
| 10 | Reliability | shop reliability (stores.yaml) + availability + community/editor/API validation |

Classes: EXCELLENT ≥90 · VERY GOOD ≥80 · GOOD ≥70 · NORMAL ≥50 · NOT INTERESTING <50.
**Expect few GOOD+ deals in the first days** — the app only trusts history it has recorded itself.

### Product matching
Same GTIN/UPC → same product. Otherwise brand, condition (new/refurbished), variant words
(Pro/Max/Ultra/Ti…), capacities, GPU/CPU/Apple chip, screen size and model codes must agree
**and** titles must be similar. When unsure, offers stay separate (a wrong merge would corrupt
history). Listings are re-verified each run, so improved rules repair old merges.

### Delivery to Yerevan (config/shipping_rates.yaml)
`product (USD) + shipping to forwarder + sales tax at origin + forwarding (chargeable kg × rate)
+ insurance (if configured) + Armenian customs`.
Customs default: **€200 / 31 kg duty-free, 15 % of the excess value, min €2 per excess kg**
(EAEU rules, Arka.am / SRC). A planned EAEU reform (5 %, min €1/kg) is included as an
alternative regime — switch `customs.active_regime` once the State Revenue Committee confirms it.

---

## Deployment (one-time)

### 1. Gmail App Password (needs you)
1. Open <https://myaccount.google.com/security> signed in as **vahehakobyanv@gmail.com**.
2. Turn on **2-Step Verification** (required for App Passwords).
3. Open <https://myaccount.google.com/apppasswords>, name it `Deal Hunter`, click **Create**.
4. Copy the 16-character password (spaces don't matter). You'll paste it in step 2.

### 2. GitHub secrets (needs you)
Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Value |
|---|---|---|
| `GMAIL_ADDRESS` | yes | `vahehakobyanv@gmail.com` |
| `GMAIL_APP_PASSWORD` | yes | the App Password from step 1 |
| `EMAIL_TO` | no | other recipients, comma-separated |
| `BESTBUY_API_KEY` | no | from developer.bestbuy.com |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | no | developer.ebay.com → Production keyset |
| `EXCHANGE_RATE_API_KEY` | no | exchangerate-api.com (fallback only) |

`GITHUB_TOKEN` is provided automatically by GitHub — do not create it.

### 3. First run
Actions tab → **Hourly deals** → **Run workflow** (tick *force_email* to get the email and Telegram summary right away, even at night). Then check:
- the run is green and the log shows `state: saved database to branch 'data'`;
- a `data` branch exists containing `deals.db.gz`;
- the email arrived (check Spam the first time and mark "Not spam");
- the next scheduled run (minute :17) logs `state: restored database`.

### 4. Reliable hourly trigger (recommended)
GitHub's cron often starts late or skips runs. A free Google Apps Script can start the
workflow every hour instead — see [scripts/google_apps_script_trigger.js](scripts/google_apps_script_trigger.js)
for the 5-step setup (needs a fine-grained GitHub token with *Actions: read & write* on this repo only).

### 5. Telegram (optional)
1. In Telegram open **@BotFather** → `/newbot` → copy the token.
2. Open your new bot and press **Start** (send it any message).
3. Add repository secret `TELEGRAM_BOT_TOKEN`, then find your chat id:
   `TELEGRAM_BOT_TOKEN=... python -m app.main telegram-setup`
4. Add repository secret `TELEGRAM_CHAT_ID`.

### Local run
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill GMAIL_APP_PASSWORD
.venv/bin/python -m pytest -q
.venv/bin/python -m app.main run --no-email     # writes data/last_report.html
.venv/bin/python -m app.main test-email          # checks Gmail settings
.venv/bin/python -m app.main stats
```

---

## Maintenance

| Task | Where |
|---|---|
| Add/remove watched products, target prices (`target_price_usd`, `min_price_usd` guard), product URLs | `config/watchlist.json` |
| Forwarder rates (Onex/Globbing/…), insurance, domestic shipping | `config/shipping_rates.yaml` |
| Customs regime | `config/shipping_rates.yaml → customs` |
| Category keywords, estimated weights, BF heuristics | `config/categories.yaml` |
| Sources, feeds, shop reliability | `config/stores.yaml` |
| Score weights, filters, email sizes, quiet hours | `config/settings.yaml` |

Edit on GitHub → commit → the `Tests` workflow validates; the next hourly run uses it.

**Free-tier notes.** Public repos have unlimited standard Actions minutes. Each run takes
~1–3 minutes. GitHub may start scheduled runs late or skip some under load — the email shows
its real time, and every run reports the latest data, so a skipped hour loses nothing. The
workflow re-enables its own schedule daily so GitHub's 60-day inactivity rule does not stop it.

**Restore a backup.** Download a `deals-db-<run>` artifact, then push it as the `data` branch:
```bash
gunzip -k backup.db.gz && mkdir -p data && mv backup.db data/deals.db && bash scripts/state.sh save
```

## Known limitations
- No historical prices exist before the first run; 30/90/180-day averages appear after 7/30/90 days.
- Pepper links open the deal page (the click-out redirect is disallowed by robots.txt).
- Weights are category estimates unless the source provides them (Best Buy does).
- Forwarder rates (Onex, 6000 AMD/kg for US/DE/UK) are set in config — update them if Onex changes prices.
- Predictions before you have a year of data are heuristic and labelled low confidence.
