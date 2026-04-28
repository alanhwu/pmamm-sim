import math


class ConstantProductAMM:
    def __init__(self, initial_liquidity: float, initial_prob: float):
        """
        initial_liquidity: Total LP position value in USDC at initial price.
        initial_prob: Starting YES probability (0 to 1).

        Reserves set so that:
            spot_price = reserve_y / reserve_x = initial_prob
            reserve_x * initial_prob + reserve_y = initial_liquidity

        This gives:
            reserve_y = initial_liquidity / 2
            reserve_x = initial_liquidity / (2 * initial_prob)
        """
        assert 0 < initial_prob < 1, f"initial_prob must be in (0,1), got {initial_prob}"
        assert initial_liquidity > 0

        self.reserve_y = initial_liquidity / 2.0
        self.reserve_x = initial_liquidity / (2.0 * initial_prob)
        self.k = self.reserve_x * self.reserve_y

        self.accumulated_fees_x = 0.0
        self.accumulated_fees_y = 0.0

        # Store initial state for PnL calculations
        self._initial_reserve_x = self.reserve_x
        self._initial_reserve_y = self.reserve_y
        self._initial_prob = initial_prob

    def spot_price(self) -> float:
        """Implied YES probability = reserve_y / reserve_x."""
        return self.reserve_y / self.reserve_x

    def initial_value(self) -> float:
        """Initial deposit value: reserve_x * initial_prob + reserve_y."""
        return self._initial_reserve_x * self._initial_prob + self._initial_reserve_y

    def compute_arb_trade(self, fair_price: float, fee: float) -> dict | None:
        """
        Compute the profit-maximizing trade for a rational trader who knows fair_price,
        given the current AMM state and fee level.

        Returns None if no profitable trade exists (spot is within the no-arb band).

        Returns dict with:
            side: "buy_yes" or "sell_yes"
            gross_input: total input from trader (including fee portion)
            net_input: input that enters reserves
            output: tokens trader receives
            fee_amount: fee collected (gross - net)
            post_spot: AMM spot price after trade
            trader_profit: trader's profit at fair_price
            new_rx: new reserve_x after trade
            new_ry: new reserve_y after trade
        """
        fair_price = max(0.001, min(0.999, fair_price))
        current_spot = self.spot_price()
        gamma = 1.0 - fee

        if gamma <= 0:
            return None  # 100% fee means no trade is profitable

        if fair_price > current_spot:
            # Case 1: Buy YES — trader buys YES, pays USDC
            # Trade only if current_spot < gamma * fair_price
            if current_spot >= gamma * fair_price - 1e-12:
                return None  # Inside no-arb band

            new_rx = math.sqrt(self.k / (gamma * fair_price))
            new_ry = self.k / new_rx

            delta_x = self.reserve_x - new_rx   # YES tokens out
            net_y_in = new_ry - self.reserve_y   # USDC entering reserves

            if net_y_in <= 1e-12 or delta_x <= 1e-12:
                return None

            # Cap at 99% of YES reserves
            if delta_x > 0.99 * self.reserve_x:
                new_rx = 0.01 * self.reserve_x
                new_ry = self.k / new_rx
                delta_x = self.reserve_x - new_rx
                net_y_in = new_ry - self.reserve_y

            gross_y_in = net_y_in / gamma
            fee_y = gross_y_in - net_y_in
            post_spot = new_ry / new_rx
            trader_profit = delta_x * fair_price - gross_y_in

            return {
                "side": "buy_yes",
                "gross_input": gross_y_in,
                "net_input": net_y_in,
                "output": delta_x,
                "fee_amount": fee_y,
                "post_spot": post_spot,
                "trader_profit": trader_profit,
                "new_rx": new_rx,
                "new_ry": new_ry,
            }

        else:
            # Case 2: Sell YES — trader sells YES, receives USDC
            # Trade only if current_spot > fair_price / gamma
            if current_spot <= fair_price / gamma + 1e-12:
                return None  # Inside no-arb band

            new_rx = math.sqrt(self.k * gamma / fair_price)
            new_ry = self.k / new_rx

            net_x_in = new_rx - self.reserve_x   # YES entering reserves
            delta_y = self.reserve_y - new_ry     # USDC out

            if net_x_in <= 1e-12 or delta_y <= 1e-12:
                return None

            # Cap at 99% of USDC reserves
            if delta_y > 0.99 * self.reserve_y:
                new_ry = 0.01 * self.reserve_y
                new_rx = self.k / new_ry
                net_x_in = new_rx - self.reserve_x
                delta_y = self.reserve_y - new_ry

            gross_x_in = net_x_in / gamma
            fee_x = gross_x_in - net_x_in
            post_spot = new_ry / new_rx
            trader_profit = delta_y - gross_x_in * fair_price

            return {
                "side": "sell_yes",
                "gross_input": gross_x_in,
                "net_input": net_x_in,
                "output": delta_y,
                "fee_amount": fee_x,
                "post_spot": post_spot,
                "trader_profit": trader_profit,
                "new_rx": new_rx,
                "new_ry": new_ry,
            }

    def execute_arb(self, fair_price: float, fee: float) -> dict | None:
        """
        Compute and execute the arb-optimal trade in one step.
        Returns None if no trade (inside no-arb band).
        Returns trade details dict if trade executed.
        """
        plan = self.compute_arb_trade(fair_price, fee)
        if plan is None:
            return None

        # Apply reserve changes
        self.reserve_x = plan["new_rx"]
        self.reserve_y = plan["new_ry"]

        # Accumulate fees
        if plan["side"] == "buy_yes":
            self.accumulated_fees_y += plan["fee_amount"]
        else:
            self.accumulated_fees_x += plan["fee_amount"]

        self._check_invariant()
        return plan

    def value_at_resolution(self, outcome: int) -> float:
        """
        Portfolio value at binary resolution.
        outcome=1: YES worth $1, USDC worth $1
        outcome=0: YES worth $0, USDC worth $1
        """
        yes_value = 1.0 if outcome == 1 else 0.0
        return (
            self.reserve_x * yes_value
            + self.reserve_y
            + self.accumulated_fees_x * yes_value
            + self.accumulated_fees_y
        )

    def _check_invariant(self):
        actual_k = self.reserve_x * self.reserve_y
        # Relative tolerance: floating point drift scales with magnitude of k
        rel_err = abs(actual_k - self.k) / self.k if self.k > 0 else abs(actual_k)
        assert rel_err < 1e-12, (
            f"Invariant violated: rx*ry={actual_k:.6f} != k={self.k:.6f}, "
            f"rel_err={rel_err:.2e}"
        )
