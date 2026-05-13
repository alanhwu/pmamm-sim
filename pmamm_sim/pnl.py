from pmamm_sim.amm import ConstantProductAMM
from pmamm_sim.types import SimResult, TradeInfo


class PnLTracker:
    def __init__(self, amm: ConstantProductAMM):
        self.initial_value = amm.initial_value()
        self.fee_revenue_yes = 0.0  # YES fees collected
        self.fee_revenue_no = 0.0   # NO fees collected
        self._cumulative_fees_usd = 0.0  # Running total of fees in USDC terms
        self.trade_log = []
        self.num_skips = 0

    def record_trade(self, trade: TradeInfo, fee_rate: float,
                     accumulated_fees_yes: float, accumulated_fees_no: float):
        """Record each trade's fee contribution and mark-to-market PnL."""
        spot_after = trade.post_spot

        if trade.side == "buy_yes":
            self.fee_revenue_no += trade.fee_amount   # Fee was in NO
            self._cumulative_fees_usd += trade.fee_amount * (1.0 - spot_after)
        else:
            self.fee_revenue_yes += trade.fee_amount  # Fee was in YES
            self._cumulative_fees_usd += trade.fee_amount * spot_after

        # Mark-to-market PnL: value YES at spot, NO at (1-spot)
        mtm_value = (
            trade.reserve_yes * spot_after
            + trade.reserve_no * (1.0 - spot_after)
            + accumulated_fees_yes * spot_after
            + accumulated_fees_no * (1.0 - spot_after)
        )
        cumulative_pnl = mtm_value - self.initial_value

        self.trade_log.append({
            "timestamp": trade.timestamp,
            "normalized_time": trade.normalized_time,
            "side": trade.side,
            "amount_yes": trade.amount_yes,
            "amount_no": trade.amount_no,
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

        In a YES/NO pool, reserves in the losing token go to $0.
        The LP's return is dominated by fees in the winning token.
        """
        terminal_value = amm.value_at_resolution(outcome)
        initial_value = self.initial_value
        total_pnl = terminal_value - initial_value

        # Pre-resolution mark-to-market
        spot = amm.spot_price()
        mtm_value = (
            amm.reserve_yes * spot
            + amm.reserve_no * (1.0 - spot)
            + amm.accumulated_fees_yes * spot
            + amm.accumulated_fees_no * (1.0 - spot)
        )

        # Fee revenue = all fees valued at MTM
        fee_revenue = (
            amm.accumulated_fees_yes * spot
            + amm.accumulated_fees_no * (1.0 - spot)
        )

        # Resolution PnL = shock from binary outcome
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
