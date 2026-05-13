import math


class ConstantProductAMM:
    def __init__(self, initial_liquidity: float, initial_prob: float):
        """
        YES/NO constant-product AMM for binary prediction markets.

        initial_liquidity: Total LP deposit in USDC. Minted into YES+NO tokens.
        initial_prob: Starting YES probability (0 to 1).

        Reserves set so that:
            spot_price = reserve_no / (reserve_yes + reserve_no) = initial_prob
            reserve_yes * initial_prob + reserve_no * (1 - initial_prob) = initial_liquidity

        This gives:
            reserve_yes = initial_liquidity / (2 * initial_prob)
            reserve_no  = initial_liquidity / (2 * (1 - initial_prob))
        """
        assert 0 < initial_prob < 1, f"initial_prob must be in (0,1), got {initial_prob}"
        assert initial_liquidity > 0

        self.reserve_yes = initial_liquidity / (2.0 * initial_prob)
        self.reserve_no = initial_liquidity / (2.0 * (1.0 - initial_prob))
        self.k = self.reserve_yes * self.reserve_no

        self.accumulated_fees_yes = 0.0
        self.accumulated_fees_no = 0.0

        # Store initial state for PnL calculations
        self._initial_reserve_yes = self.reserve_yes
        self._initial_reserve_no = self.reserve_no
        self._initial_prob = initial_prob

    def spot_price(self) -> float:
        """Implied YES probability = reserve_no / (reserve_yes + reserve_no)."""
        return self.reserve_no / (self.reserve_yes + self.reserve_no)

    def initial_value(self) -> float:
        """Initial deposit value in USDC."""
        return (
            self._initial_reserve_yes * self._initial_prob
            + self._initial_reserve_no * (1.0 - self._initial_prob)
        )

    def compute_arb_trade(self, fair_price: float, fee: float) -> dict | None:
        """
        Compute the profit-maximizing trade for a rational trader who knows fair_price,
        given the current AMM state and fee level.

        In this YES/NO pool, traders swap YES for NO or NO for YES.

        Returns None if no profitable trade exists (spot is within the no-arb band).
        """
        fair_price = max(0.001, min(0.999, fair_price))
        gamma = 1.0 - fee

        if gamma <= 0:
            return None

        current_ratio = self.reserve_no / self.reserve_yes
        fair_ratio = fair_price / (1.0 - fair_price)
        current_spot = self.spot_price()

        if fair_price > current_spot:
            # Case 1: Buy YES — trader gives NO, receives YES
            # Trade only if current_ratio < gamma * fair_ratio
            if current_ratio >= gamma * fair_ratio - 1e-12:
                return None  # Inside no-arb band

            new_reserve_no = math.sqrt(self.k * gamma * fair_price / (1.0 - fair_price))
            new_reserve_yes = self.k / new_reserve_no

            delta_yes = self.reserve_yes - new_reserve_yes  # YES tokens out
            net_no_in = new_reserve_no - self.reserve_no     # NO entering reserves

            if net_no_in <= 1e-12 or delta_yes <= 1e-12:
                return None

            # Cap at 99% of YES reserves
            if delta_yes > 0.99 * self.reserve_yes:
                new_reserve_yes = 0.01 * self.reserve_yes
                new_reserve_no = self.k / new_reserve_yes
                delta_yes = self.reserve_yes - new_reserve_yes
                net_no_in = new_reserve_no - self.reserve_no

            gross_no_in = net_no_in / gamma
            fee_no = gross_no_in - net_no_in
            post_spot = new_reserve_no / (new_reserve_yes + new_reserve_no)
            # Profit: YES gained * fair_yes_usd - NO spent * fair_no_usd
            trader_profit = delta_yes * fair_price - gross_no_in * (1.0 - fair_price)

            return {
                "side": "buy_yes",
                "gross_input": gross_no_in,
                "net_input": net_no_in,
                "output": delta_yes,
                "fee_amount": fee_no,
                "post_spot": post_spot,
                "trader_profit": trader_profit,
                "new_reserve_yes": new_reserve_yes,
                "new_reserve_no": new_reserve_no,
            }

        else:
            # Case 2: Sell YES — trader gives YES, receives NO
            # Trade only if current_ratio > fair_ratio / gamma
            if current_ratio <= fair_ratio / gamma + 1e-12:
                return None  # Inside no-arb band

            new_reserve_yes = math.sqrt(self.k * gamma * (1.0 - fair_price) / fair_price)
            new_reserve_no = self.k / new_reserve_yes

            net_yes_in = new_reserve_yes - self.reserve_yes  # YES entering reserves
            delta_no = self.reserve_no - new_reserve_no       # NO tokens out

            if net_yes_in <= 1e-12 or delta_no <= 1e-12:
                return None

            # Cap at 99% of NO reserves
            if delta_no > 0.99 * self.reserve_no:
                new_reserve_no = 0.01 * self.reserve_no
                new_reserve_yes = self.k / new_reserve_no
                net_yes_in = new_reserve_yes - self.reserve_yes
                delta_no = self.reserve_no - new_reserve_no

            gross_yes_in = net_yes_in / gamma
            fee_yes = gross_yes_in - net_yes_in
            post_spot = new_reserve_no / (new_reserve_yes + new_reserve_no)
            trader_profit = delta_no * (1.0 - fair_price) - gross_yes_in * fair_price

            return {
                "side": "sell_yes",
                "gross_input": gross_yes_in,
                "net_input": net_yes_in,
                "output": delta_no,
                "fee_amount": fee_yes,
                "post_spot": post_spot,
                "trader_profit": trader_profit,
                "new_reserve_yes": new_reserve_yes,
                "new_reserve_no": new_reserve_no,
            }

    def execute_arb(self, fair_price: float, fee: float) -> dict | None:
        """
        Compute and execute the arb-optimal trade in one step.
        Returns None if no trade (inside no-arb band).
        """
        plan = self.compute_arb_trade(fair_price, fee)
        if plan is None:
            return None

        self.reserve_yes = plan["new_reserve_yes"]
        self.reserve_no = plan["new_reserve_no"]

        # Accumulate fees in the input token
        if plan["side"] == "buy_yes":
            self.accumulated_fees_no += plan["fee_amount"]   # Fee was in NO
        else:
            self.accumulated_fees_yes += plan["fee_amount"]  # Fee was in YES

        self._check_invariant()
        return plan

    def value_at_resolution(self, outcome: int) -> float:
        """
        Portfolio value at binary resolution.

        outcome=1 (YES wins): YES worth $1, NO worth $0
        outcome=0 (NO wins):  YES worth $0, NO worth $1

        In practice, reserves in the winning token are near zero (arbed away),
        so the return is dominated by fees in the winning token.
        """
        if outcome == 1:
            return self.reserve_yes + self.accumulated_fees_yes
        else:
            return self.reserve_no + self.accumulated_fees_no

    def _check_invariant(self):
        actual_k = self.reserve_yes * self.reserve_no
        rel_err = abs(actual_k - self.k) / self.k if self.k > 0 else abs(actual_k)
        assert rel_err < 1e-12, (
            f"Invariant violated: ry*rn={actual_k:.6f} != k={self.k:.6f}, "
            f"rel_err={rel_err:.2e}"
        )
