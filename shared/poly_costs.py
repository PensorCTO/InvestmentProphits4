import math
import os


class PolyCostModel:
    """
    The Absolute Truth of the IP4 Arena.
    Defines synthetic spread and slippage based on Polymarket liquidity tiers.
    Strictly Paper Execution.
    """

    BASELINE_FRICTION_BPS = 0.0015  # 15 bps — Middle ground for Autoresearch

    # Base spread when crossing the book (Ask - Bid)
    TIER_SPREADS = {
        "HIGH_LIQUIDITY": 0.005,  # e.g., Major US Election, >$10M volume
        "MED_LIQUIDITY": 0.015,   # e.g., Standard Pop Culture/Crypto, $1M-$10M
        "LOW_LIQUIDITY": 0.035,   # e.g., Niche Science/Micro-events, <$1M
    }

    TIER_SYNTHETIC_VOLUME = {
        "HIGH_LIQUIDITY": 10_000_000,
        "MED_LIQUIDITY": 1_000_000,
        "LOW_LIQUIDITY": 500_000,
    }

    TIER_SLIPPAGE_MULTIPLIERS = {
        "HIGH_LIQUIDITY": 1.0,
        "MED_LIQUIDITY": 2.0,
        "LOW_LIQUIDITY": 4.0,
    }

    TIER_DEPTH_FRACTION = {
        "HIGH_LIQUIDITY": 0.15,
        "MED_LIQUIDITY": 0.08,
        "LOW_LIQUIDITY": 0.03,
    }

    # Slippage penalty per $100 deployed (Impact model)
    SLIPPAGE_COEFF = 0.002
    DEPTH_SLIPPAGE_K = float(os.getenv("DEPTH_SLIPPAGE_K", "2.5"))

    _PRICE_FLOOR = 0.01
    _PRICE_CEIL = 0.99

    @classmethod
    def _to_logit(cls, p: float) -> float:
        p = max(cls._PRICE_FLOOR, min(cls._PRICE_CEIL, p))
        return math.log(p / (1.0 - p))

    @classmethod
    def _from_logit(cls, x: float) -> float:
        p = 1.0 / (1.0 + math.exp(-x))
        return max(cls._PRICE_FLOOR, min(cls._PRICE_CEIL, p))

    @classmethod
    def _slippage(
        cls,
        bet_size: float,
        liquidity_tier: str,
        *,
        capital: float | None = None,
    ) -> float:
        mult = cls.TIER_SLIPPAGE_MULTIPLIERS.get(liquidity_tier, 4.0)
        base = (bet_size / 100.0) * cls.SLIPPAGE_COEFF * mult
        if capital is None or capital <= 0:
            return base
        kelly_fraction = bet_size / capital
        depth_frac = cls.TIER_DEPTH_FRACTION.get(liquidity_tier, 0.03)
        penetration = max(0.0, kelly_fraction / depth_frac - 1.0)
        penetration = min(penetration, 3.0)
        slippage = base * math.exp(cls.DEPTH_SLIPPAGE_K * penetration)
        return min(slippage, 0.05)

    @classmethod
    def tier_meets_liquidity_floor(cls, liquidity_tier: str, liquidity_floor: float) -> bool:
        synthetic_volume = cls.TIER_SYNTHETIC_VOLUME.get(liquidity_tier, 500_000)
        return synthetic_volume >= liquidity_floor

    @classmethod
    def _tier_thresholds(cls) -> tuple[float, float]:
        high = float(
            os.getenv("TIER_HIGH_VOLUME", str(cls.TIER_SYNTHETIC_VOLUME["HIGH_LIQUIDITY"]))
        )
        med = float(
            os.getenv("TIER_MED_VOLUME", str(cls.TIER_SYNTHETIC_VOLUME["MED_LIQUIDITY"]))
        )
        return high, med

    @classmethod
    def infer_tier_from_volume(cls, volume_usd: float) -> str:
        """Map Polymarket volume to IP4 liquidity tier labels."""
        high, med = cls._tier_thresholds()
        if volume_usd >= high:
            return "HIGH_LIQUIDITY"
        if volume_usd >= med:
            return "MED_LIQUIDITY"
        return "LOW_LIQUIDITY"

    @classmethod
    def infer_tier_from_signals(
        cls,
        *,
        volume_usd: float = 0.0,
        liquidity_usd: float = 0.0,
        book_notional: float = 0.0,
    ) -> str:
        """Best tier from Gamma volume, Gamma liquidity, and live CLOB book notional."""
        effective = max(
            float(volume_usd or 0.0),
            float(liquidity_usd or 0.0),
            float(book_notional or 0.0),
        )
        return cls.infer_tier_from_volume(effective)

    @classmethod
    def infer_tier_from_book_notional(cls, bid_depth: float, ask_depth: float, mid: float) -> str:
        """Fallback tier from order-book notional when Gamma volume unavailable."""
        notional = (bid_depth + ask_depth) * mid
        return cls.infer_tier_from_signals(book_notional=notional)

    @classmethod
    def compute_brackets(
        cls,
        entry_price: float,
        *,
        direction: str = "YES",
        tp_logit_delta: float = 1.2,
        sl_logit_delta: float = 0.8,
    ) -> tuple[float, float]:
        """
        Log-odds stop/take-profit bounded in [0.01, 0.99] price space.
        """
        entry = max(cls._PRICE_FLOOR, min(cls._PRICE_CEIL, entry_price))
        logit = cls._to_logit(entry)

        stop_loss = cls._from_logit(logit - sl_logit_delta)
        take_profit = cls._from_logit(logit + tp_logit_delta)
        stop_loss = min(stop_loss, max(cls._PRICE_FLOOR, entry - 0.001))
        take_profit = max(take_profit, min(cls._PRICE_CEIL, entry + 0.001))
        if stop_loss >= entry:
            stop_loss = max(0.001, entry * 0.5)

        return stop_loss, take_profit

    @classmethod
    def calculate_net_edge(
        cls,
        fair_value: float,
        market_mid: float,
        liquidity_tier: str,
        bet_size: float,
        *,
        capital: float | None = None,
    ) -> float:
        """
        Calculates the true mathematical edge of a signal.
        Edge_net = |fair - mid| - (half_spread + slippage)
        """
        raw_edge = abs(fair_value - market_mid)

        half_spread = cls.TIER_SPREADS.get(liquidity_tier, 0.035) / 2.0
        slippage = cls._slippage(bet_size, liquidity_tier, capital=capital)

        net_edge = raw_edge - (half_spread + slippage + cls.BASELINE_FRICTION_BPS)
        return net_edge

    @classmethod
    def calculate_directional_net_edge(
        cls,
        fair_value: float,
        market_mid: float,
        direction: str,
        liquidity_tier: str,
        bet_size: float,
        *,
        capital: float | None = None,
    ) -> float:
        """Signed edge in trade direction after spread + slippage at fill price."""
        fill = cls.get_execution_price(
            market_mid, direction, liquidity_tier, bet_size, capital=capital
        )
        if direction == "YES":
            return fair_value - fill - cls.BASELINE_FRICTION_BPS
        return (1.0 - fair_value) - fill - cls.BASELINE_FRICTION_BPS

    @classmethod
    def get_position_exit_price(
        cls,
        direction: str,
        market_mid: float,
        liquidity_tier: str,
        bet_size: float,
        *,
        capital: float | None = None,
    ) -> float:
        """
        Bid-side mark for closing an open YES/NO position in the same price
        space as entry_price and compute_brackets (not the opposite token).
        """
        half_spread = cls.TIER_SPREADS.get(liquidity_tier, 0.035) / 2.0
        slippage = cls._slippage(bet_size, liquidity_tier, capital=capital)
        penalty = half_spread + slippage
        yes_mid = max(cls._PRICE_FLOOR, min(cls._PRICE_CEIL, market_mid))
        if direction == "YES":
            return max(1e-6, yes_mid - penalty)
        no_mid = max(cls._PRICE_FLOOR, min(cls._PRICE_CEIL, 1.0 - yes_mid))
        return max(1e-6, no_mid - penalty)

    @classmethod
    def get_execution_price(
        cls,
        market_mid: float,
        direction: str,
        liquidity_tier: str,
        bet_size: float,
        *,
        capital: float | None = None,
    ) -> float:
        """
        Calculates the exact paper-fill price, penalizing the agent for crossing the spread.
        """
        half_spread = cls.TIER_SPREADS.get(liquidity_tier, 0.035) / 2.0
        slippage = cls._slippage(bet_size, liquidity_tier, capital=capital)

        penalty = half_spread + slippage

        if direction == "YES":
            fill_price = market_mid + penalty
            return max(0.01, min(fill_price, 0.99))
        fill_price = (1.0 - market_mid) + penalty
        return max(0.01, min(fill_price, 0.99))
