import json
from pmamm_sim.types import MarketTrade


def load_polymarket_trades(json_path: str) -> tuple[list[MarketTrade], dict]:
    """
    Load and preprocess Polymarket trade data.

    Collapses multi-leg transactions into one price observation per tx_hash.
    The Dome API returns both maker and taker sides of every fill, so the
    net YES shares per tx is always ~0. We don't filter on net direction —
    every tx is a valid price observation, and the AMM's
    compute_trade_to_target_price determines direction from current spot vs target.

    Collapse logic:
    - Group by tx_hash
    - Take the max yes_price in the group as the target (for multi-price fills,
      this is the most extreme level reached; 80%+ of groups share a single price)
    - Halve USD volume (maker+taker double counting)

    Returns:
        trades: list of MarketTrade sorted by timestamp
        metadata: summary statistics about the loaded data
    """
    with open(json_path, "r") as f:
        raw_data = json.load(f)

    total_raw = len(raw_data)

    # Group by tx_hash
    groups: dict[str, list[dict]] = {}
    for entry in raw_data:
        tx = entry.get("tx_hash", "")
        groups.setdefault(tx, []).append(entry)

    trades = []

    for tx_hash, legs in groups.items():
        timestamp = legs[0]["timestamp"]

        # Take the max yes_price as the target price for this observation.
        # For single-price groups (majority), this is trivially correct.
        # For multi-price fills, this captures the most extreme fill level.
        yes_prices = [leg["yes_price"] for leg in legs]
        yes_price = max(yes_prices)

        # USD volume: sum all legs, halve (maker+taker double counting)
        total_usd = sum(leg["usd_size"] for leg in legs)
        usd_volume = total_usd / 2.0

        trades.append(MarketTrade(
            timestamp=timestamp,
            yes_price=yes_price,
            usd_volume=usd_volume,
        ))

    # Sort by timestamp
    trades.sort(key=lambda t: t.timestamp)

    # Build metadata
    timestamps = [t.timestamp for t in trades]
    prices = [t.yes_price for t in trades]

    metadata = {
        "total_raw_trades": total_raw,
        "total_collapsed_trades": len(trades),
        "skipped_neutral_txs": 0,
        "date_range": (min(timestamps), max(timestamps)) if timestamps else (0, 0),
        "yes_price_range": (min(prices), max(prices)) if prices else (0.0, 0.0),
    }

    return trades, metadata
