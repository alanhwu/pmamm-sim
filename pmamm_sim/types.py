from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FeeQuote:
    bid_fee: float  # Fee when trader sells YES (gives YES, gets NO)
    ask_fee: float  # Fee when trader buys YES (gives NO, gets YES)


@dataclass(frozen=True)
class PendingTrade:
    """What the strategy sees BEFORE a trade executes (for beforeSwap hook)."""
    side: str                  # "buy_yes" or "sell_yes" (from trader's perspective)
    fair_price: float          # Where the market believes price should be
    current_spot: float        # AMM's current spot price before trade
    reserve_yes: float         # Current YES reserves
    reserve_no: float          # Current NO reserves
    timestamp: int             # Unix timestamp
    time_to_resolution: float  # Seconds until market resolves
    normalized_time: float     # 0.0 (market open) to 1.0 (resolution)


@dataclass(frozen=True)
class TradeInfo:
    """What the strategy sees AFTER a trade executes (for afterSwap hook)."""
    side: str                  # "buy_yes" or "sell_yes" (from trader's perspective)
    amount_yes: float          # YES shares traded
    amount_no: float           # NO shares traded
    fee_amount: float          # Fee collected on this trade (in input token)
    timestamp: int
    reserve_yes: float         # Post-trade YES reserves
    reserve_no: float          # Post-trade NO reserves
    time_to_resolution: float
    normalized_time: float
    fair_price: float          # The fair value signal from historical data
    post_spot: float           # AMM's spot price after trade (differs from fair_price)
    realized_price: float      # Implied YES probability of execution price


@dataclass
class MarketTrade:
    """A single preprocessed trade observation from Polymarket."""
    timestamp: int
    yes_price: float           # Target YES price after this trade (0 to 1)
    usd_volume: float          # Approximate USD volume of the original trade


@dataclass
class SimResult:
    fee_revenue: float             # Total fees collected, in USDC terms
    resolution_pnl: float          # Gain/loss from binary resolution (excl. fees)
    total_pnl: float               # fee_revenue + resolution_pnl
    return_on_liquidity: float     # total_pnl / initial_liquidity
    initial_liquidity: float
    num_trades: int                # Trades that executed
    num_trades_skipped: int = 0    # Trades inside no-arb band
    skip_rate: float = 0.0         # skipped / (executed + skipped)
    trade_log: list = field(default_factory=list)


@runtime_checkable
class StrategyProtocol(Protocol):
    """Formal interface for fee strategies.

    Any class with before_swap(PendingTrade) -> FeeQuote and
    after_swap(TradeInfo | None) -> None satisfies this protocol.
    """
    def before_swap(self, pending: PendingTrade) -> FeeQuote: ...
    def after_swap(self, trade: TradeInfo | None) -> None: ...


def validate_strategy(instance: object, label: str = "strategy") -> None:
    """Raise TypeError if instance lacks required before_swap / after_swap methods."""
    import inspect
    for method_name in ("before_swap", "after_swap"):
        method = getattr(instance, method_name, None)
        if method is None or not callable(method):
            raise TypeError(f"{label}: missing callable method '{method_name}'")
        sig = inspect.signature(method)
        # bound method: 'self' is already bound, so expect exactly 1 parameter
        if len(sig.parameters) != 1:
            raise TypeError(
                f"{label}.{method_name} expects 1 parameter, "
                f"got {len(sig.parameters)}"
            )
