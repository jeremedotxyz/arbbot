# arbbot

Paper triangular-arbitrage scanner for Kraken. No API keys, no real money — it only reads public prices and logs what it *would* have traded.

## What it does

Polls Kraken's public Ticker, builds every 3-asset triangle it can trade (e.g. USD → BTC → ETH → USD), and flags the ones that would still profit **after** Kraken's 0.26% taker fee on each of the 3 legs (0.78% total drag).

- **132 triangles** across 16 liquid assets (BTC, ETH, SOL, USDT, USDC, XRP, ADA, DOT, LTC, DOGE, LINK, AVAX, ATOM, UNI, XLM)
- Paper mode: virtual $1,000 balance, $100 notional per flagged opportunity
- Paper trades logged to `paper_trades.jsonl`

## Run it

```bash
python3 -m pip install requests   # once
python3 triangular_arb_scanner.py --duration 300    # 5 minutes
python3 triangular_arb_scanner.py --duration 3600   # an hour
python3 triangular_arb_scanner.py --once            # one snapshot
python3 triangular_arb_scanner.py --threshold 0.02  # looser bar
```

## Honest limitations

- Top-of-book prices only. A real fill eats into the book, so slippage on a $100 notional is small but on a $10k notional it is not.
- It assumes all 3 legs fill instantly at the quoted prices. In reality the gap can close between leg 1 and leg 3, leaving you holding the middle asset. That is the main risk of triangular arb.
- Fee used is Kraken's default taker rate (0.26%). Volume discounts would lower it.
- If it prints nothing for minutes, that is normal: on a liquid exchange, after-fee triangular gaps are rare and tiny. Zeroes mean the scanner is working, not broken.

First live test (Nov 23, 2026): 60 seconds, 16 polls, 0 paper trades — best triangle was −0.73% net after fees.
