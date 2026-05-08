# pmamm-sim Competition Guide

You're being given a link to a hosted competition site. Your job: write a fee strategy for a prediction market AMM that maximizes LP returns across a diverse set of real Polymarket markets.

## The challenge

You control the **fees** on a constant-product AMM (like Uniswap, but for binary prediction markets). Each market has a stream of real historical trades. A rational arbitrageur pushes the AMM price toward the market's fair value — but stops when your fee eats into their profit.

Your strategy decides: **how much to charge on each trade?**

- Charge too little → you capture volume but earn thin margins
- Charge too much → trades get skipped (they fall inside the "no-arbitrage band") and you earn nothing
- The sweet spot depends on volatility, time to resolution, and market dynamics

Your strategy is scored by **average return on liquidity** across all markets. The leaderboard ranks everyone's strategies against the same data.

## How to submit

Go to the competition site and paste your strategy code. Fill in a name, hit **Submit & Run**, and watch the leaderboard update. That's it.

## Writing your strategy

Your strategy is a Python class with two hooks:

```python
class Strategy:
    def __init__(self):
        # Set up state. Called once per market, no arguments.
        self.fee = 100 / 10_000  # 100 bps

    def before_swap(self, pending):
        # Return FeeQuote(bid_fee, ask_fee) — fees as decimals.
        # 0.01 = 1% = 100 bps
        return FeeQuote(bid_fee=self.fee, ask_fee=self.fee)

    def after_swap(self, trade):
        # Update state. trade is None if the trade was skipped.
        pass
```

`FeeQuote`, `PendingTrade`, and `TradeInfo` are automatically available — you don't need to import them.

## What you can see

**In `before_swap(pending)`** — information available *before* you set fees:

| Field | What it is |
|-------|------------|
| `pending.side` | `"buy_yes"` or `"sell_yes"` — which direction the trade will go |
| `pending.fair_price` | The external market's current fair value (0 to 1) |
| `pending.current_spot` | Your AMM's spot price right now |
| `pending.reserve_x` | YES tokens in the pool |
| `pending.reserve_y` | USDC in the pool |
| `pending.timestamp` | Unix timestamp |
| `pending.time_to_resolution` | Seconds until the market resolves |
| `pending.normalized_time` | 0.0 = market just opened, 1.0 = resolution |

**In `after_swap(trade)`** — what happened (or `None` if skipped):

| Field | What it is |
|-------|------------|
| `trade.side` | Direction of the executed trade |
| `trade.amount_x` | YES shares that moved |
| `trade.amount_y` | USDC that moved |
| `trade.fee_amount` | Fee you earned on this trade |
| `trade.reserve_x` / `reserve_y` | Pool reserves after the trade |
| `trade.fair_price` | Fair value signal for this trade |
| `trade.post_spot` | Your AMM's spot price after the trade |
| `trade.realized_price` | Actual execution price (amount_y / amount_x) |
| `trade.normalized_time` | Same as above — useful for time-based state updates |

## Key concepts

**No-arbitrage band.** If your fee is high enough that the arbitrageur can't profit, the trade gets skipped. This is tracked as your **skip rate**. A 90% skip rate means 90% of price signals were inside your fee band — you only traded on the 10% with large price moves.

**Bid vs ask fees.** `bid_fee` is charged when the trader sells YES to you. `ask_fee` is charged when the trader buys YES from you. You can make them asymmetric (e.g., charge more on the side where you're getting adversely selected).

**Resolution PnL.** At the end, the market resolves YES or NO. If it resolves YES, your YES reserves and YES fees are worth $1 each. If NO, they're worth $0. Strategies that accumulate lots of YES fees do great on YES-resolution markets but get crushed on NO-resolution markets. The best strategies are robust across both.

**Fresh state per market.** `__init__` is called once per market. State you build up (like an EWMA tracker) resets between markets but persists across all trades within a single market.

## Baselines to beat

The built-in strategies give you reference points:

| Strategy | Avg Return | Approach |
|----------|-----------|----------|
| FixedFee(50bps) | ~+56% | Constant 0.5% fee |
| FixedFee(100bps) | ~+87% | Constant 1% fee |
| FixedFee(200bps) | ~+120% | Constant 2% fee |
| EWMAMomentumFee | ~+123% | Asymmetric fees tracking price momentum + time ramp |

These numbers come from the test dataset bundled with the repo. The competition server runs against a larger set of markets, so exact numbers will differ — but the relative ordering gives you intuition.

## Ideas to explore

- **Time decay** — Adverse selection (informed trading) gets worse as resolution approaches. Ramp fees up near the end.
- **Volatility tracking** — EWMA of recent price changes. Widen fees when the market is choppy.
- **Asymmetric fees** — If price is trending up, maybe charge less on the buy side (follow the trend) and more on the sell side.
- **Regime detection** — Different fee levels for "calm" vs "volatile" periods based on recent trade patterns.
- **Reserve imbalance** — If the pool is heavily tilted toward YES, charge more on buys to rebalance.

## Developing locally

Clone the repo and test against the bundled dataset (12 markets):

```bash
git clone https://github.com/alanhwu/pmamm-sim.git
cd pmamm-sim

# Test your strategy via CLI
python -m pmamm_sim compete ./submissions/

# Or run the local web UI
python -m pmamm_sim serve
# Open http://localhost:8080/compete.html
```

The repo comes with 12 real Polymarket markets covering geopolitics, Fed decisions, sports, and commodities. Use these to develop and debug your strategy locally. The competition server runs against a broader set of markets — a strategy that overfits to 12 markets won't necessarily win.

## Rules

- Your code must define a class with `before_swap` and `after_swap` methods
- Imports are restricted to: `math`, `statistics`, `collections`, `dataclasses`, `functools`, `itertools`, and `pmamm_sim.types`
- No file access, no network, no `exec`/`eval`, no dunder attribute tricks
- The strategy must be instantiable with no arguments
- Have fun with it
