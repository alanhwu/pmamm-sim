from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeeQuote:
    bid_fee: float  # Fee when AMM buys YES (trader sells YES to pool)
    ask_fee: float  # Fee when AMM sells YES (trader buys YES from pool)


@dataclass(frozen=True)
class PendingTrade:
    """What the strategy sees BEFORE a trade executes (for beforeSwap hook)."""
    side: str                  # "buy_yes" or "sell_yes" (from trader's perspective)
    fair_price: float          # Where the market believes price should be
    current_spot: float        # AMM's current spot price before trade
    reserve_x: float           # Current YES reserves
    reserve_y: float           # Current USDC reserves
    timestamp: int             # Unix timestamp
    time_to_resolution: float  # Seconds until market resolves
    normalized_time: float     # 0.0 (market open) to 1.0 (resolution)


@dataclass(frozen=True)
class TradeInfo:
    """What the strategy sees AFTER a trade executes (for afterSwap hook)."""
    side: str                  # "buy_yes" or "sell_yes" (from trader's perspective)
    amount_x: float            # YES shares traded
    amount_y: float            # USDC traded
    fee_amount: float          # Fee collected on this trade (in input token)
    timestamp: int
    reserve_x: float           # Post-trade YES reserves
    reserve_y: float           # Post-trade USDC reserves
    time_to_resolution: float
    normalized_time: float
    fair_price: float          # The fair value signal from historical data
    post_spot: float           # AMM's spot price after trade (differs from fair_price)
    realized_price: float      # Actual execution price (amount_y / amount_x)


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
