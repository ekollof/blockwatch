"""Fiat estimates for watched balances.

One CoinGecko request covers every supported coin and the selected fiat
currency. Rates are cached in the state file for five minutes; a rate failure
never fails an address check. The request contains symbols and a currency code,
never addresses, labels or keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from . import http
from .util import BlockwatchError, iso_from_string, plain

FIATS = {
    "USD": {"symbol": "$", "decimals": 2},
    "EUR": {"symbol": "€", "decimals": 2},
    "GBP": {"symbol": "£", "decimals": 2},
    "JPY": {"symbol": "¥", "decimals": 0},
    "CAD": {"symbol": "CA$", "decimals": 2},
    "AUD": {"symbol": "A$", "decimals": 2},
    "CHF": {"symbol": "CHF", "decimals": 2},
    "CNY": {"symbol": "CN¥", "decimals": 2},
    "INR": {"symbol": "₹", "decimals": 2},
}

COIN_IDS = {
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "DOGE": "dogecoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XMR": "monero",
}

RATE_TTL_SECONDS = 300


def normalize_fiat(value):
    code = str(value or "USD").upper()
    return code if code in FIATS else "USD"


def fiat_list():
    return [
        {"code": code, "symbol": info["symbol"], "decimals": info["decimals"]}
        for code, info in FIATS.items()
    ]


def fetch_rates(fiat):
    code = normalize_fiat(fiat)
    ids = ",".join(COIN_IDS[symbol] for symbol in COIN_IDS)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies={code.lower()}&precision=full")
    document = http.request_json(url, timeout=15, max_bytes=65536)
    if not isinstance(document, dict):
        raise BlockwatchError("rate provider returned an unexpected document")
    values = {}
    for symbol, coin_id in COIN_IDS.items():
        row = document.get(coin_id)
        if not isinstance(row, dict):
            continue
        raw = row.get(code.lower())
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            continue
        try:
            rate = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if not rate.is_finite() or rate <= 0:
            continue
        rendered = format(rate, "f")
        if len(rendered) > 40:
            continue
        values[symbol] = rendered
    if not values:
        raise BlockwatchError("rate provider returned no usable rates")
    return values


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_cache(raw):
    if not isinstance(raw, dict):
        return None
    currency = normalize_fiat(raw.get("currency"))
    fetched = iso_from_string(raw.get("fetchedAt"))
    values = raw.get("values")
    if not fetched or not isinstance(values, dict):
        return None
    cleaned = {}
    for symbol in COIN_IDS:
        value = values.get(symbol)
        if not isinstance(value, str) or len(value) > 40:
            continue
        try:
            rate = Decimal(value)
        except (InvalidOperation, ValueError):
            continue
        if rate.is_finite() and rate > 0:
            cleaned[symbol] = format(rate, "f")
    if not cleaned:
        return None
    return {"currency": currency, "fetchedAt": fetched, "values": cleaned}


def _cache_age(fetched_at, now=None):
    fetched = iso_from_string(fetched_at)
    if not fetched:
        return None
    try:
        then = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - then).total_seconds()))


def _report(cache, fiat, error=None, stale=False):
    info = FIATS[normalize_fiat(fiat)]
    return {
        "currency": normalize_fiat(fiat),
        "symbol": info["symbol"],
        "decimals": info["decimals"],
        "fetchedAt": cache["fetchedAt"] if cache else None,
        "stale": stale,
        "values": dict(cache["values"]) if cache else {},
        "error": plain(error, 200) if error else None,
    }


def rates_for(state, fiat, fetch=False, now=None):
    """Return a report, updating state['rates'] only with a fresh fetch."""
    fiat = normalize_fiat(fiat)
    cache = clean_cache(state.get("rates"))
    if cache is not None and cache["currency"] != fiat:
        cache = None
    age = _cache_age(cache["fetchedAt"], now) if cache else None
    fresh = cache is not None and age is not None and age <= RATE_TTL_SECONDS
    if not fetch or fresh:
        return _report(cache, fiat, stale=cache is not None and not fresh)
    try:
        values = fetch_rates(fiat)
    except BlockwatchError as error:
        if cache is not None:
            return _report(cache, fiat, error=str(error), stale=True)
        return _report(None, fiat, error=str(error))
    state["rates"] = {"currency": fiat, "fetchedAt": _now(), "values": values}
    return _report(state["rates"], fiat)


def fiat_fields(balance_raw, crypto_decimals, symbol, rates):
    """Fields for one address row. rates is a report from rates_for()."""
    currency = rates.get("currency") if rates else None
    info = FIATS.get(normalize_fiat(currency)) if currency else None
    fields = {
        "fiatValue": None,
        "fiatCurrency": currency,
        "fiatSymbol": info["symbol"] if info else None,
        "fiatDecimals": info["decimals"] if info else None,
    }
    if balance_raw is None or not rates or not rates.get("values") or not info:
        return fields
    rate_raw = rates["values"].get(symbol)
    if not rate_raw:
        return fields
    try:
        amount = Decimal(str(balance_raw)) / (Decimal(10) ** int(crypto_decimals))
        value = amount * Decimal(rate_raw)
        quantum = Decimal(1).scaleb(-int(info["decimals"]))
        value = value.quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return fields
    if not value.is_finite() or value < 0:
        return fields
    rendered = format(value, f".{int(info['decimals'])}f")
    if len(rendered) > 60:
        return fields
    fields["fiatValue"] = rendered
    return fields
