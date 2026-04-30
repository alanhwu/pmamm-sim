"""
Strategy Submission Template
============================

To create a competition entry:
1. Copy this file and remove the leading underscore from the filename.
2. Rename it to something descriptive (e.g., my_strategy.py).
3. Implement the Strategy class below.
4. Run: python -m pmamm_sim compete ./submissions/

Rules:
- Your file MUST contain a class named 'Strategy'.
- Strategy() must be instantiable with NO arguments.
- It must implement:
    before_swap(pending: PendingTrade) -> FeeQuote
    after_swap(trade: TradeInfo | None) -> None
- You may import from: standard library, pmamm_sim.types.
- A fresh Strategy instance is created for each market.

Optional module-level metadata:
- STRATEGY_NAME: str  -- display name on the leaderboard (default: filename)
- AUTHOR: str         -- your name (default: "anonymous")
- DESCRIPTION: str    -- one-line description

PendingTrade fields (available in before_swap):
    side              "buy_yes" or "sell_yes" (trader's perspective)
    fair_price        Where the market believes price should be (0-1)
    current_spot      AMM's current spot price before trade
    reserve_x         Current YES reserves
    reserve_y         Current USDC reserves
    timestamp         Unix timestamp
    time_to_resolution  Seconds until market resolves
    normalized_time   0.0 (market open) to 1.0 (resolution)

TradeInfo fields (available in after_swap, or None if skipped):
    side, amount_x, amount_y, fee_amount, timestamp,
    reserve_x, reserve_y, time_to_resolution, normalized_time,
    fair_price, post_spot, realized_price
"""

from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo

# Optional metadata
STRATEGY_NAME = "MyStrategy"
AUTHOR = "Your Name"
DESCRIPTION = "Describe what your strategy does in one line."


class Strategy:
    """Your fee strategy. Instantiated fresh for each market."""

    def __init__(self):
        # Initialize any state you need here.
        # This is called once per market, with no arguments.
        self.base_fee = 100 / 10_000  # 100 bps = 1%

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        """Return the bid/ask fees to charge for this trade.

        Fees are decimals: 0.01 = 1% = 100 bps.
        """
        return FeeQuote(bid_fee=self.base_fee, ask_fee=self.base_fee)

    def after_swap(self, trade: TradeInfo | None) -> None:
        """Update internal state after a trade (or skip).

        trade is None when the trade was inside the no-arb band (skipped).
        """
        pass
