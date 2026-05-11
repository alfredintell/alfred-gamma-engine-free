# Alfred Gamma Engine — Free Build

This is the no-cost workaround for real option-derived levels without MenthorQ.

## Important truth
TradingView Pine cannot directly fetch a full options chain or read a random GitHub/JSON URL. New Pine Seeds repository creation is currently unavailable, so fully automatic Pine chart ingestion is blocked unless you already have access to an active Pine Seeds repo.

This repo still sets up the free data engine:

- Pulls free option-chain data from Yahoo Finance through `yfinance`.
- Uses QQQ as the free proxy for NQ/MNQ and SPY as the free proxy for ES/MES.
- Converts ETF option strikes to futures-price levels using a live futures/ETF ratio.
- Calculates Call Wall, Put Wall, HVL/Gamma Flip, 0DTE/front-expiry walls, Gamma Wall, Expected Move, and GEX1-GEX10.
- Saves clean JSON/CSV outputs for Alfred Terminal, a web dashboard, manual TradingView backup inputs, or a future Pine Seeds/custom feed.

## Accuracy model

### Free proxy mode
This is automatic and free, but it is a proxy:

- NQ/MNQ uses QQQ options.
- ES/MES uses SPY options.
- Levels are scaled to the futures chart.

This is not identical to CME futures options gamma, but it is far better than fake current-price ladders.

### Exact futures mode
For exact CME futures options levels, the engine needs CME futures-options chain data by strike/expiration/OI/IV. Free CME public pages can show OI/strike structure, but automated machine access is limited. Paid APIs are the clean route. If you later get CME/Databento/exported CSV data, the code is structured so a CME loader can be added.

## Run locally

```bash
cd alfred_gamma_engine_free
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/alfred_gamma_engine.py --market NQ --output out
python src/alfred_gamma_engine.py --market ES --output out
```

Outputs:

- `out/NQ_levels.json`
- `out/NQ_levels.csv`
- `out/NQ_pine_manual_backup.txt`
- `out/ES_levels.json`
- `out/ES_levels.csv`
- `out/ES_pine_manual_backup.txt`

## GitHub Actions setup

1. Create a GitHub repo named `alfred-gamma-engine-free`.
2. Upload all files from this folder.
3. Go to **Actions** and enable workflows.
4. Run **Update Alfred Gamma Levels** manually first.
5. The workflow will commit updated files in `/out`.

The workflow runs every trading morning and can be triggered manually.

## TradingView integration reality

### What works today
- Alfred Terminal or the included web page can automatically read `out/NQ_levels.json` from GitHub raw.
- Pine can still display levels if you paste the generated `*_pine_manual_backup.txt` values into Inputs.

### What is blocked
- Pine cannot directly read arbitrary JSON/CSV URLs.
- Pine Seeds is the official TradingView path for GitHub custom EOD data, but new Pine Seeds repositories are currently unavailable.

### If Pine Seeds returns or you already have a repo
Use one symbol per level, e.g. `NQ_CALL_WALL`, `NQ_PUT_WALL`, `NQ_HVL`, then request them in Pine with:

```pine
callWall = request.seed("seed_YOURNAME_gamma", "NQ_CALL_WALL", close)
```

## Level definitions

- Call Wall / Call Resistance: strongest call gamma cluster above price.
- Put Wall / Put Support: strongest put gamma cluster below price.
- HVL / Gamma Flip: closest cumulative net gamma zero-crossing zone.
- Front/0DTE Call Wall: strongest call gamma cluster in nearest expiry.
- Front/0DTE Put Wall: strongest put gamma cluster in nearest expiry.
- Gamma Wall: strongest total absolute gamma strike.
- Expected Move: ATM straddle when available, otherwise IV fallback.
- GEX1-GEX10: top absolute gamma exposure levels ranked by size.

## Files

- `src/alfred_gamma_engine.py` — main calculator.
- `.github/workflows/update-gamma.yml` — scheduled free auto update.
- `web/index.html` — simple Alfred Gamma dashboard.
- `pine/alfred_gamma_request_seed_template.pine` — template for future Pine Seeds.
- `browser/tradingview_overlay.user.js` — optional dashboard overlay panel, not true Pine lines.
