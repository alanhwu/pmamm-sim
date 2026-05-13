from pmamm_sim.amm import ConstantProductAMM
from pmamm_sim.pnl import PnLTracker
from pmamm_sim.types import MarketTrade, PendingTrade, TradeInfo


class SimulationEngine:
    def run_single(
        self,
        trades: list[MarketTrade],
        strategy,
        initial_liquidity: float,
        initial_prob: float,
        outcome: int,
        market_start: int,
        market_end: int,
    ) -> "SimResult":
        """Run one strategy through one AMM and return a SimResult."""
        amm = ConstantProductAMM(initial_liquidity, initial_prob)
        tracker = PnLTracker(amm)

        duration = market_end - market_start
        if duration <= 0:
            duration = 1

        for trade in trades:
            normalized_time = (trade.timestamp - market_start) / duration
            normalized_time = max(0.0, min(1.0, normalized_time))
            time_to_resolution = max(0, market_end - trade.timestamp)
            self._process_single_trade(
                amm, strategy, tracker, trade,
                normalized_time, time_to_resolution,
            )

        # Final resolution arb: outcome is known, someone arbs to 99/1
        resolution_price = 0.999 if outcome == 1 else 0.001
        resolution_trade = MarketTrade(
            timestamp=market_end, yes_price=resolution_price, usd_volume=0.0,
        )
        self._process_single_trade(
            amm, strategy, tracker, resolution_trade,
            normalized_time=1.0, time_to_resolution=0,
        )

        return tracker.compute_final(amm, outcome)

    def run_replay(
        self,
        trades: list[MarketTrade],
        test_strategy,
        baseline_strategy,
        initial_liquidity: float,
        initial_prob: float,
        outcome: int,
        market_start: int,
        market_end: int,
    ) -> dict:
        """
        Replay the same trade sequence through two independent AMMs and compare results.

        Returns dict with "test", "baseline", and "comparison" keys.
        """
        amm_test = ConstantProductAMM(initial_liquidity, initial_prob)
        amm_baseline = ConstantProductAMM(initial_liquidity, initial_prob)
        pnl_test = PnLTracker(amm_test)
        pnl_baseline = PnLTracker(amm_baseline)

        duration = market_end - market_start
        if duration <= 0:
            duration = 1  # avoid division by zero

        for trade in trades:
            normalized_time = (trade.timestamp - market_start) / duration
            normalized_time = max(0.0, min(1.0, normalized_time))
            time_to_resolution = max(0, market_end - trade.timestamp)

            for amm, strategy, tracker in [
                (amm_test, test_strategy, pnl_test),
                (amm_baseline, baseline_strategy, pnl_baseline),
            ]:
                self._process_single_trade(
                    amm, strategy, tracker, trade,
                    normalized_time, time_to_resolution,
                )

        # Final resolution arb for both AMMs
        resolution_price = 0.999 if outcome == 1 else 0.001
        resolution_trade = MarketTrade(
            timestamp=market_end, yes_price=resolution_price, usd_volume=0.0,
        )
        for amm, strategy, tracker in [
            (amm_test, test_strategy, pnl_test),
            (amm_baseline, baseline_strategy, pnl_baseline),
        ]:
            self._process_single_trade(
                amm, strategy, tracker, resolution_trade,
                normalized_time=1.0, time_to_resolution=0,
            )

        result_test = pnl_test.compute_final(amm_test, outcome)
        result_baseline = pnl_baseline.compute_final(amm_baseline, outcome)

        baseline_ret = result_baseline.return_on_liquidity
        test_ret = result_test.return_on_liquidity
        improvement = test_ret - baseline_ret

        # Relative improvement: how much of baseline's loss was recovered (or gain changed)
        if abs(baseline_ret) > 1e-10:
            improvement_pct = improvement / abs(baseline_ret) * 100
        else:
            improvement_pct = 0.0

        return {
            "test": result_test,
            "baseline": result_baseline,
            "comparison": {
                "test_return": test_ret,
                "baseline_return": baseline_ret,
                "improvement": improvement,
                "improvement_pct": improvement_pct,
            },
        }

    def _process_single_trade(
        self,
        amm: ConstantProductAMM,
        strategy,
        tracker: PnLTracker,
        trade: MarketTrade,
        normalized_time: float,
        time_to_resolution: float,
    ):
        """Process one historical trade signal through one AMM+strategy pair.

        The historical trade's yes_price is treated as fair value. A rational
        trader pushes the AMM toward fair value but stops early because the fee
        eats into profit. Higher fees -> earlier stop -> more skipped trades.
        """
        fair_price = trade.yes_price
        fair_price = max(0.001, min(0.999, fair_price))

        current_spot = amm.spot_price()

        # Quick check: no price movement at all
        if abs(current_spot - fair_price) < 1e-10:
            tracker.record_skip()
            return

        side = "buy_yes" if fair_price > current_spot else "sell_yes"

        # 1. Build PendingTrade for beforeSwap
        pending = PendingTrade(
            side=side,
            fair_price=fair_price,
            current_spot=current_spot,
            reserve_yes=amm.reserve_yes,
            reserve_no=amm.reserve_no,
            timestamp=trade.timestamp,
            time_to_resolution=time_to_resolution,
            normalized_time=normalized_time,
        )

        # 2. beforeSwap: strategy sets fee
        fee_quote = strategy.before_swap(pending)
        fee = fee_quote.ask_fee if side == "buy_yes" else fee_quote.bid_fee

        # 3. Execute arb-optimal trade with the fee
        result = amm.execute_arb(fair_price, fee)

        if result is None:
            # Inside no-arb band: fee too high for profitable trade
            tracker.record_skip()
            strategy.after_swap(None)
            return

        # 4. Build TradeInfo for afterSwap
        if side == "buy_yes":
            amount_yes = result["output"]       # YES out to trader
            amount_no = result["gross_input"]    # NO in from trader
        else:
            amount_yes = result["gross_input"]   # YES in from trader
            amount_no = result["output"]         # NO out to trader

        total_tokens = amount_yes + amount_no
        realized_price = amount_no / total_tokens if total_tokens > 0 else 0.0

        trade_info = TradeInfo(
            side=side,
            amount_yes=amount_yes,
            amount_no=amount_no,
            fee_amount=result["fee_amount"],
            timestamp=trade.timestamp,
            reserve_yes=amm.reserve_yes,
            reserve_no=amm.reserve_no,
            time_to_resolution=time_to_resolution,
            normalized_time=normalized_time,
            fair_price=fair_price,
            post_spot=result["post_spot"],
            realized_price=realized_price,
        )

        # 5. afterSwap: strategy updates state
        strategy.after_swap(trade_info)

        # 6. Track PnL
        tracker.record_trade(
            trade_info, fee_rate=fee,
            accumulated_fees_yes=amm.accumulated_fees_yes,
            accumulated_fees_no=amm.accumulated_fees_no,
        )
