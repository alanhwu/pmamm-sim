from pmamm_sim.amm import ConstantProductAMM
from pmamm_sim.types import SimResult, TradeInfo


class PnLTracker:
    def __init__(self, amm: ConstantProductAMM):
        self.initial_value = amm.initial_value()
        self.fee_revenue_y = 0.0  # USDC fees collected
        self.fee_revenue_x = 0.0  # YES fees collected
        self._cumulative_fees_usd = 0.0  # Running total of fees in USDC terms
        self.trade_log = []
        self.num_skips = 0

    def record_trade(self, trade: TradeInfo, fee_rate: float,
                     accumulated_fees_x: float, accumulated_fees_y: float):
        """Record each trade's fee contribution and mark-to-market PnL."""
        spot_after = trade.post_spot

        if trade.side == "buy_yes":
            self.fee_revenue_y += trade.fee_amount  # Fee was in USDC
            self._cumulative_fees_usd += trade.fee_amount
        else:
            self.fee_revenue_x += trade.fee_amount  # Fee was in YES tokens
            self._cumulative_fees_usd += trade.fee_amount * spot_after

        # Mark-to-market PnL: value everything at current spot
        mtm_value = (
            trade.reserve_x * spot_after
            + trade.reserve_y
            + accumulated_fees_x * spot_after
            + accumulated_fees_y
        )
        cumulative_pnl = mtm_value - self.initial_value

        self.trade_log.append({
            "timestamp": trade.timestamp,
            "normalized_time": trade.normalized_time,
            "side": trade.side,
            "amount_x": trade.amount_x,
            "amount_y": trade.amount_y,
            "fee": trade.fee_amount,
            "fee_rate": fee_rate,
            "fair_price": trade.fair_price,
            "post_spot": trade.post_spot,
            "cumulative_fees": self._cumulative_fees_usd,
            "cumulative_pnl": cumulative_pnl,
        })

    def record_skip(self):
        """Record a trade signal that fell inside the no-arb band."""
        self.num_skips += 1

    def compute_final(self, amm: ConstantProductAMM, outcome: int) -> SimResult:
        """Compute terminal PnL including binary resolution.

        Decomposition:
          fee_revenue:    all fees collected, valued at pre-resolution spot (MTM).
          resolution_pnl: the shock from binary outcome -- the jump from MTM to
                          actual terminal value.  Differs per strategy because
                          strategies accumulate different YES fee balances that
                          get wiped (outcome=0) or crystallised (outcome=1).
          total_pnl:      terminal_value - initial_value (the actual bottom line).
        """
        terminal_value = amm.value_at_resolution(outcome)
        initial_value = self.initial_value
        total_pnl = terminal_value - initial_value

        # Pre-resolution mark-to-market: value YES at current spot
        spot = amm.spot_price()
        mtm_value = (
            amm.reserve_x * spot
            + amm.reserve_y
            + amm.accumulated_fees_x * spot
            + amm.accumulated_fees_y
        )

        # Fee revenue = all fees valued at MTM (what the strategy earned)
        fee_revenue = amm.accumulated_fees_y + amm.accumulated_fees_x * spot

        # Resolution PnL = shock from binary outcome on reserves + fees
        # = (outcome - spot) * (reserve_x + accumulated_fees_x)
        resolution_pnl = terminal_value - mtm_value

        num_executed = len(self.trade_log)
        total_signals = num_executed + self.num_skips
        skip_rate = self.num_skips / total_signals if total_signals > 0 else 0.0

        return SimResult(
            fee_revenue=fee_revenue,
            resolution_pnl=resolution_pnl,
            total_pnl=total_pnl,
            return_on_liquidity=total_pnl / initial_value,
            initial_liquidity=initial_value,
            num_trades=num_executed,
            num_trades_skipped=self.num_skips,
            skip_rate=skip_rate,
            trade_log=self.trade_log,
        )
