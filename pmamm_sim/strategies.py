from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo


class FixedFee:
    """Constant symmetric fee regardless of market conditions."""

    def __init__(self, fee_bps: float = 100):
        self.fee = fee_bps / 10_000

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        return FeeQuote(bid_fee=self.fee, ask_fee=self.fee)

    def after_swap(self, trade: TradeInfo | None) -> None:
        pass


class TimeDecayFee:
    """Fee increases as resolution approaches.

    Intuition: informed trading (adverse selection) accelerates near resolution,
    so LP protection should too.
    """

    def __init__(self, base_bps: float = 50, max_bps: float = 500, ramp_start: float = 0.7):
        self.base = base_bps / 10_000
        self.max = max_bps / 10_000
        self.ramp_start = ramp_start

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        t = pending.normalized_time
        if t <= self.ramp_start:
            fee = self.base
        else:
            progress = (t - self.ramp_start) / (1.0 - self.ramp_start)
            fee = self.base + (self.max - self.base) * progress
        fee = min(fee, self.max)
        return FeeQuote(bid_fee=fee, ask_fee=fee)

    def after_swap(self, trade: TradeInfo | None) -> None:
        pass


class VolatilityAwareFee:
    """Tracks recent price movement and widens fees when volatility is high."""

    def __init__(self, base_bps: float = 50, vol_multiplier: float = 5000,
                 lookback: int = 20, max_bps: float = 500):
        self.base = base_bps / 10_000
        self.max = max_bps / 10_000
        self.vol_multiplier = vol_multiplier / 10_000
        self.lookback = lookback

        # EWMA state
        self._ewma_vol = 0.0
        self._alpha = 3.0 / (lookback + 1)
        self._last_price = None
        self._initialized = False

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        fee = self.base + self.vol_multiplier * self._ewma_vol
        fee = min(fee, self.max)
        return FeeQuote(bid_fee=fee, ask_fee=fee)

    def after_swap(self, trade: TradeInfo | None) -> None:
        if trade is None:
            return
        price = trade.post_spot
        if self._last_price is not None:
            abs_change = abs(price - self._last_price)
            if self._initialized:
                self._ewma_vol = self._alpha * abs_change + (1 - self._alpha) * self._ewma_vol
            else:
                self._ewma_vol = abs_change
                self._initialized = True
        self._last_price = price


class CombinedFee:
    """Weighted combination of time-decay and volatility signals."""

    def __init__(self, base_bps: float = 50, time_weight: float = 0.5,
                 vol_weight: float = 0.5, max_bps: float = 500,
                 ramp_start: float = 0.7, vol_lookback: int = 20,
                 vol_multiplier: float = 5000):
        self.base = base_bps / 10_000
        self.max = max_bps / 10_000
        self.time_weight = time_weight
        self.vol_weight = vol_weight
        self.ramp_start = ramp_start

        # Time-decay component
        self._max_time = max_bps / 10_000

        # Volatility component
        self._vol_multiplier = vol_multiplier / 10_000
        self._ewma_vol = 0.0
        self._alpha = 2.0 / (vol_lookback + 1)
        self._last_price = None
        self._initialized = False

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        # Time component
        t = pending.normalized_time
        if t <= self.ramp_start:
            time_fee = 0.0
        else:
            progress = (t - self.ramp_start) / (1.0 - self.ramp_start)
            time_fee = (self._max_time - self.base) * progress

        # Volatility component
        vol_fee = self._vol_multiplier * self._ewma_vol

        # Combined
        fee = self.base + self.time_weight * time_fee + self.vol_weight * vol_fee
        fee = min(fee, self.max)
        return FeeQuote(bid_fee=fee, ask_fee=fee)

    def after_swap(self, trade: TradeInfo | None) -> None:
        if trade is None:
            return
        price = trade.post_spot
        if self._last_price is not None:
            abs_change = abs(price - self._last_price)
            if self._initialized:
                self._ewma_vol = self._alpha * abs_change + (1 - self._alpha) * self._ewma_vol
            else:
                self._ewma_vol = abs_change
                self._initialized = True
        self._last_price = price


class EWMAMomentumFee:
    """Asymmetric fee strategy based on EWMA price-ratio tracking.

    Ported and enhanced from the on-chain 'joy' strategy.  Tracks an
    exponentially weighted moving average of the reserve ratio (Y/X) and
    adjusts bid/ask fees asymmetrically based on the current ratio's
    deviation from the EWMA.

    When the price is above the EWMA (uptrend):
      - ask fee (trader buys YES) is *reduced*  -- follow the trend
      - bid fee (trader sells YES) is *increased* -- discourage fading

    Enhancement over the original Solidity version: fees are now computed
    in before_swap (which wasn't available on-chain) so the pending-trade
    context -- including time-to-resolution -- can be used for an additional
    adaptive time ramp that widens fees as resolution approaches.
    """

    def __init__(
        self,
        base_bps: float = 35,
        max_adjustment_bps: float = 200,
        alpha: float = 0.03,
        scale: float = 0.55,
        time_ramp_start: float = 0.80,
        time_ramp_max_bps: float = 100,
    ):
        self.base_fee = base_bps / 10_000
        self.max_adjustment = max_adjustment_bps / 10_000
        self.alpha = alpha
        self.scale = scale
        self.time_ramp_start = time_ramp_start
        self.time_ramp_max = time_ramp_max_bps / 10_000

        # EWMA state -- initialised on the first trade
        self._ewma_ratio: float | None = None

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _clamp(fee: float) -> float:
        return max(0.0, min(fee, 0.50))

    def _time_bump(self, normalized_time: float) -> float:
        """Extra fee component that ramps up near resolution."""
        if normalized_time <= self.time_ramp_start:
            return 0.0
        progress = (normalized_time - self.time_ramp_start) / (1.0 - self.time_ramp_start)
        return self.time_ramp_max * progress

    # -- hooks ------------------------------------------------------------

    def before_swap(self, pending: PendingTrade) -> FeeQuote:
        ratio = (
            pending.reserve_no / pending.reserve_yes
            if pending.reserve_yes > 0
            else pending.fair_price / (1.0 - pending.fair_price)
        )

        # First trade: initialise EWMA, return symmetric base + time bump
        if self._ewma_ratio is None:
            self._ewma_ratio = ratio
            fee = self._clamp(self.base_fee + self._time_bump(pending.normalized_time))
            return FeeQuote(bid_fee=fee, ask_fee=fee)

        # Core asymmetric logic (direct port from Solidity)
        bid_fee = self.base_fee
        ask_fee = self.base_fee

        if ratio > self._ewma_ratio:
            raw_dev = (ratio - self._ewma_ratio) / self._ewma_ratio
            adjustment = min(raw_dev * self.scale, self.max_adjustment)
            ask_fee = max(self.base_fee - adjustment, 0.0)
            bid_fee = self.base_fee + adjustment
        elif ratio < self._ewma_ratio:
            raw_dev = (self._ewma_ratio - ratio) / self._ewma_ratio
            adjustment = min(raw_dev * self.scale, self.max_adjustment)
            bid_fee = max(self.base_fee - adjustment, 0.0)
            ask_fee = self.base_fee + adjustment

        # Time-to-resolution bump (new -- not available on-chain)
        time_bump = self._time_bump(pending.normalized_time)
        bid_fee += time_bump
        ask_fee += time_bump

        return FeeQuote(bid_fee=self._clamp(bid_fee), ask_fee=self._clamp(ask_fee))

    def after_swap(self, trade: TradeInfo | None) -> None:
        if trade is None:
            return
        ratio = (
            trade.reserve_no / trade.reserve_yes
            if trade.reserve_yes > 0
            else trade.fair_price / (1.0 - trade.fair_price)
        )
        if self._ewma_ratio is None:
            self._ewma_ratio = ratio
        else:
            self._ewma_ratio = self.alpha * ratio + (1.0 - self.alpha) * self._ewma_ratio


STRATEGIES = {
    "fixed": FixedFee,
    "time_decay": TimeDecayFee,
    "volatility": VolatilityAwareFee,
    "combined": CombinedFee,
    "ewma_momentum": EWMAMomentumFee,
}

STRATEGY_REGISTRY = [
    {"name": "FixedFee(50bps)",        "class": FixedFee,            "kwargs": {"fee_bps": 50}},
    {"name": "FixedFee(100bps)",       "class": FixedFee,            "kwargs": {"fee_bps": 100}},
    {"name": "FixedFee(200bps)",       "class": FixedFee,            "kwargs": {"fee_bps": 200}},
    {"name": "TimeDecayFee",           "class": TimeDecayFee,        "kwargs": {}},
    {"name": "VolatilityAwareFee",     "class": VolatilityAwareFee,  "kwargs": {}},
    {"name": "CombinedFee",            "class": CombinedFee,         "kwargs": {}},
    {"name": "EWMAMomentumFee",        "class": EWMAMomentumFee,     "kwargs": {}},
]
