# pmamm-sim Competition Guide

You're being given a link to a hosted competition site. Your job: write a fee strategy for a prediction market AMM that maximizes LP returns across a diverse set of real Polymarket markets.

## The challenge

You control the **fees** on a constant-product AMM (like Uniswap, but for binary prediction markets). Each market has a stream of real historical trades. A rational arbitrageur pushes the AMM price toward the market's fair value — but stops when your fee eats into their profit.

Your strategy decides: **how much to charge on each trade?**

- Charge too little: you capture volume but earn thin margins
- Charge too much: trades get skipped (they fall inside the "no-arbitrage band") and you earn nothing
- The sweet spot depends on volatility, time to resolution, and market dynamics

Your strategy is scored by **median return on liquidity** across all markets. The leaderboard ranks everyone's strategies against the same data.

---

## Submitting on the competition site

1. Open the competition link you were given
2. Write your strategy code in the editor (see "Writing your strategy" below)
3. Give it a name and your name
4. Click **Submit & Run**
5. Your submission is queued and gets a job id on the server
6. The page polls for status updates (`queued` -> `running` -> `succeeded`/`failed`/`timed_out`) and refreshes the leaderboard when done

`FeeQuote`, `PendingTrade`, and `TradeInfo` are automatically available in the web editor — you don't need to import them.

### Runtime limit

- Each submission has a **120-second execution limit** once it starts running.
- If a run exceeds this limit, it is marked `timed_out` and does not update the leaderboard.

---

## Developing and testing locally

If you want to iterate faster, clone the repo and test locally. The repo ships with 12 real Polymarket markets you can test against. The competition server runs against a larger set, so treat the local data as a dev environment.

### Setup

```bash
git clone https://github.com/alanhwu/pmamm-sim.git
cd pmamm-sim
pip install -r requirements.txt
```

### Option A: local web UI

```bash
python -m pmamm_sim serve
# Open http://localhost:8080/compete.html
```

Same interface as the competition site, running against the local dataset.

### Option B: file-based workflow

1. Copy the template:

```bash
cp submissions/_template.py submissions/my_strategy.py
```

2. Edit `submissions/my_strategy.py` — implement your strategy in the `Strategy` class. Set the `STRATEGY_NAME` and `AUTHOR` variables at the top of the file.

3. Run the competition locally:

```bash
python -m pmamm_sim compete ./submissions/
```

This sweeps your strategy across all bundled markets and prints a leaderboard. Built-in strategies are included by default. Pass `--no-builtins` to exclude them:

```bash
python -m pmamm_sim compete ./submissions/ --no-builtins
```

4. For detailed per-market charts, run the server and open the visualizer:

```bash
python -m pmamm_sim serve
# http://localhost:8080/visualizer.html
```

### File format

Your submission file should look like this:

```python
from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo

STRATEGY_NAME = "MyStrategy"
AUTHOR = "Your Name"
DESCRIPTION = "One-line description of your approach"


class Strategy:
    def __init__(self):
        self.fee = 100 / 10_000

    def before_swap(self, pending):
        return FeeQuote(bid_fee=self.fee, ask_fee=self.fee)

    def after_swap(self, trade):
        pass
```

The class can be named anything (doesn't have to be `Strategy`) — the loader finds it automatically by looking for `before_swap` and `after_swap` methods. Files starting with `_` are skipped (that's why `_template.py` doesn't run).

---

## Writing your strategy

Your strategy is a Python class with two hooks:

```python
class Strategy:
    def __init__(self):
        # Set up state. Called once per market, no arguments.
        self.fee = 100 / 10_000  # 100 bps

    def before_swap(self, pending):
        # Decide what fee to charge. Return FeeQuote(bid_fee, ask_fee).
        # Fees are decimals: 0.01 = 1% = 100 bps.
        return FeeQuote(bid_fee=self.fee, ask_fee=self.fee)

    def after_swap(self, trade):
        # Update internal state. trade is None if the trade was skipped.
        pass
```

### What you can see

**In `before_swap(pending)`** — information available *before* you set fees:

| Field | What it is |
|-------|------------|
| `pending.side` | `"buy_yes"` or `"sell_yes"` — which direction the trade will go |
| `pending.fair_price` | The external market's current fair value (0 to 1) |
| `pending.current_spot` | Your AMM's spot price right now |
| `pending.reserve_yes` | YES tokens in the pool |
| `pending.reserve_no` | NO tokens in the pool |
| `pending.timestamp` | Unix timestamp |
| `pending.time_to_resolution` | Seconds until the market resolves |
| `pending.normalized_time` | 0.0 = market just opened, 1.0 = resolution |

**In `after_swap(trade)`** — what happened (or `None` if skipped):

| Field | What it is |
|-------|------------|
| `trade.side` | Direction of the executed trade |
| `trade.amount_yes` | YES shares that moved |
| `trade.amount_no` | NO shares that moved |
| `trade.fee_yes` | Fee earned in YES tokens (>0 on sell_yes, 0 on buy_yes) |
| `trade.fee_no` | Fee earned in NO tokens (>0 on buy_yes, 0 on sell_yes) |
| `trade.reserve_yes` / `reserve_no` | Pool reserves after the trade |
| `trade.fair_price` | Fair value signal for this trade |
| `trade.post_spot` | Your AMM's spot price after the trade |
| `trade.realized_price` | Implied YES probability of execution price |
| `trade.normalized_time` | Same time field — useful for time-based state updates |

---

## Key concepts

**No-arbitrage band.** If your fee is high enough that the arbitrageur can't profit, the trade gets skipped. This is tracked as your **skip rate**. A 90% skip rate means 90% of price signals were inside your fee band — you only traded on the 10% with large enough price moves.

**Bid vs ask fees.** `bid_fee` is charged when the trader sells YES to you. `ask_fee` is charged when the trader buys YES from you. You can make them asymmetric (e.g., charge more on the side where you're getting adversely selected).

**Resolution PnL.** At the end, the market resolves YES or NO. The pool holds YES and NO tokens; the losing side goes to $0. Arbitrageurs drain the winning token from the pool as the price approaches 0 or 1, so reserves are worth ~$0 at resolution. The only LP return is **fees in the winning token**. If you accumulated 600 YES fees and 400 NO fees, and YES wins, your return is $600. The NO fees are worthless. The best strategies accumulate fees on both sides to be robust across outcomes.

**Fresh state per market.** `__init__` is called once per market with no arguments. State you build up (like an EWMA tracker) resets between markets but persists across all trades within one market.

---

## Ideas to explore

- **Time decay** — Adverse selection (informed trading) gets worse as resolution approaches. Ramp fees up near the end.
- **Volatility tracking** — EWMA of recent price changes. Widen fees when the market is choppy.
- **Asymmetric fees** — If price is trending up, maybe charge less on the buy side (follow the trend) and more on the sell side.
- **Regime detection** — Different fee levels for "calm" vs "volatile" periods based on recent trade patterns.
- **Reserve imbalance** — If the pool is heavily tilted toward YES, charge more on buys to rebalance.

---

## Rules

- Your code must define a class with `before_swap` and `after_swap` methods
- Use one consistent `AUTHOR` / author name for all your submissions. Do not use aliases, abbreviations, or variations to bypass the 2-submissions-per-author limit.
- Imports are restricted to: `math`, `statistics`, `collections`, `dataclasses`, `functools`, `itertools`, and `pmamm_sim.types`
- No file access, no network, no `exec`/`eval`, no dunder attribute tricks
- The strategy must be instantiable with no arguments
- Have fun with it
