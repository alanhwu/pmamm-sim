"""Minimal working submission: fixed 100 bps fee."""

from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo

STRATEGY_NAME = "ExampleFixedFee(100bps)"
AUTHOR = "pmamm-sim"
DESCRIPTION = "Reference submission -- fixed symmetric 100 bps fee."


class Strategy:
    def __init__(self):
        self.fee = 100 / 10_000

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        return FeeQuote(bid_fee=self.fee, ask_fee=self.fee)

    def after_swap(self, trade: TradeInfo | None) -> None:
        pass
