"""What each indicator and term means, as the app uses it. The analyst answers
"how does X work" from this text, and the chart page shows it. Kept short, factual
and tied to this app's own implementation in app/indicators/core.py."""

INDICATORS: dict[str, dict[str, str]] = {
    "EMA": {
        "what": "Exponential moving average: an average of closing prices that weights recent bars more. "
                "This app seeds it with the simple average of the first n bars, like TradingView.",
        "use": "Trend direction and dynamic support/resistance. EMA 20 above EMA 50 is a common uptrend filter.",
        "limits": "Lags price; in a sideways market price crosses it back and forth (whipsaws).",
    },
    "SMA": {
        "what": "Simple moving average: the plain average of the last n closes.",
        "use": "Smoother but slower than an EMA; 20, 50 and 200 periods are common.",
        "limits": "Even more lag than an EMA.",
    },
    "VWAP": {
        "what": "Volume-weighted average price for the session: the average price paid, weighted by volume, "
                "reset every morning at 9:15.",
        "use": "Intraday fair value. Institutions benchmark against it; price above VWAP is often read as buyers "
               "in control. Used by the VWAP pullback strategy.",
        "limits": "Needs volume, so indices (Nifty, Bank Nifty) have none; use the futures instead. Meaningless on daily bars.",
    },
    "RSI": {
        "what": "Relative Strength Index (Wilder, 14 bars): compares the size of recent up moves with down moves, "
                "scaled 0 to 100.",
        "use": "Above 70 is called overbought, below 30 oversold; also used for divergences.",
        "limits": "In a strong trend RSI can stay overbought or oversold for a long time; it is not a reversal signal by itself.",
    },
    "MACD": {
        "what": "EMA 12 minus EMA 26 (the MACD line), a 9-bar EMA of that line (the signal line), and their "
                "difference (the histogram).",
        "use": "Momentum and trend changes: the MACD line crossing its signal line, or the histogram changing sign.",
        "limits": "Built from moving averages, so it lags; many false crosses in sideways markets.",
    },
    "Supertrend": {
        "what": "A trailing line placed a multiple of ATR (default 10 bars × 3) below price in an uptrend or above it "
                "in a downtrend; it flips when price closes through it.",
        "use": "Trend following and trailing stops. Used by the Supertrend strategy (15-minute bars).",
        "limits": "Whipsaws in ranges; late entries after big moves; the stop can be far away.",
    },
    "Bollinger Bands": {
        "what": "A 20-bar SMA with bands 2 standard deviations above and below.",
        "use": "Volatility: narrow bands (a squeeze) often come before big moves; touches of the bands are used for "
               "mean reversion in ranges.",
        "limits": "A close outside a band is not a reversal signal in a trend; price can 'walk the band'.",
    },
    "ATR": {
        "what": "Average True Range (Wilder, 14 bars): how far price typically moves in one bar, gaps included.",
        "use": "Sizing stops to the market's noise (e.g. 1.5 × ATR), and comparing volatility between stocks.",
        "limits": "Says nothing about direction.",
    },
    "Opening range": {
        "what": "The high and low of the first 15 minutes (9:15 to 9:30). This app draws it only after it is complete.",
        "use": "Opening-range breakout: trade the first 5-minute close outside it, stop at its midpoint.",
        "limits": "Many false breaks on choppy days; this app's backtests show it losing after costs on large caps in the "
                  "last three months.",
    },
    "Previous day levels": {
        "what": "Yesterday's high, low and close.",
        "use": "Common support and resistance; breaks of the previous day's high or low are a classic setup.",
        "limits": "Widely watched, so stops cluster around them.",
    },
    "India VIX": {
        "what": "NSE's volatility index: the market's expected Nifty volatility over the next 30 days, from option prices.",
        "use": "This app tags every trade with yesterday's VIX (low <13, normal 13–18, high >18).",
        "limits": "It measures expected movement, not direction.",
    },
    "R multiple": {
        "what": "A trade's result divided by the money at risk (entry to stop × quantity). +2R made twice what was risked; "
                "-1R lost exactly the planned amount.",
        "use": "Compares trades of different sizes; average R after costs (expectancy) is how this app judges a strategy.",
        "limits": "In this app R is after charges, so a trade stopped exactly at its stop shows about -1.05R to -1.1R; "
                  "that is the charges, not slippage. Gaps through the stop can make it worse.",
    },
}

TERMS = (
    "Paper trading in this app: simulated fills on free, delayed 1-minute data with Zerodha's charges. "
    "MIS: Zerodha's intraday product; Zerodha squares MIS positions off from 3:25 PM, so this app exits by your flat-by time. "
    "Confidence on a signal card comes from a fixed rule (number of trades and average R after costs), never from the model."
)


def as_text() -> str:
    lines = []
    for name, d in INDICATORS.items():
        lines.append(f"{name}: {d['what']} Use: {d['use']} Limits: {d['limits']}")
    return "\n".join(lines) + "\n" + TERMS


R_NOTE = "R in this app is measured AFTER charges: a trade stopped exactly at its stop shows about -1.05R to -1.1R because of Zerodha's charges (and a little slippage); that is normal, not an execution problem. "
