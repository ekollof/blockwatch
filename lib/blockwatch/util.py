"""Small, dependency-free helpers shared by the Blockwatch helper.

Everything here is deliberately narrow: parse an untrusted JSON document,
validate a URL, format an atomic amount, or turn a timestamp into ISO UTC.
Network access itself lives in :mod:`blockwatch.http`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

MAX_TEXT = 200


class BlockwatchError(Exception):
    """A failure that should be reported to the user, not a traceback."""


class ValidationError(BlockwatchError):
    pass


def require_dict(value, what):
    if not isinstance(value, dict):
        raise ValidationError(f"{what} is not a JSON object")
    return value


def require_list(value, what, limit=500):
    if not isinstance(value, list):
        raise ValidationError(f"{what} is not a JSON array")
    if len(value) > limit:
        raise ValidationError(f"{what} has more than {limit} entries")
    return value


def text(value, what, limit=MAX_TEXT, allow_empty=False):
    """A display string from untrusted data: bounded, no control characters."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{what} is not a string")
    cleaned = "".join(ch for ch in value if ch >= " " and ch != "\x7f"
                      and not (0x80 <= ord(ch) <= 0x9F)
                      and not _is_bidi_control(ch)).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
    if not cleaned and not allow_empty:
        raise ValidationError(f"{what} is empty")
    return cleaned


def _is_bidi_control(ch):
    code = ord(ch)
    return (0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069
            or code in (0x200E, 0x200F))


def plain(value, limit=MAX_TEXT):
    """Display text without raising: bounded, control and bidi controls gone."""
    if value is None:
        return ""
    text_value = str(value)
    return "".join(ch for ch in text_value if ch >= " " and ch != "\x7f"
                   and not (0x80 <= ord(ch) <= 0x9F)
                   and not _is_bidi_control(ch)).strip()[:limit]


def digit_string(value, what, limit=80):
    """A non-negative integer amount, kept as a decimal string."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{what} is not an integer")
    rendered = str(value)
    if not rendered or len(rendered) > limit or not rendered.isdigit():
        raise ValidationError(f"{what} is not a non-negative integer")
    return rendered


def int_value(value, what, minimum=0, maximum=10**30):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{what} is not an integer")
    if value < minimum or value > maximum:
        raise ValidationError(f"{what} is out of range")
    return value


def bool_value(value, what):
    if not isinstance(value, bool):
        raise ValidationError(f"{what} is not a boolean")
    return value


def format_units(raw, decimals):
    """Atomic units to a plain decimal string, trailing zeros trimmed."""
    raw = digit_string(raw, "amount")
    try:
        amount = Decimal(raw) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError) as error:
        raise ValidationError("amount cannot be formatted") from error
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def format_delta(raw, decimals):
    """Like :func:`format_units`, but keeps a sign for balance deltas."""
    raw = str(raw)
    sign = ""
    if raw.startswith("-"):
        sign, raw = "-", raw[1:]
    return sign + format_units(raw, decimals)


def iso_utc(epoch):
    if epoch is None:
        return None
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        raise ValidationError("timestamp is not a number")
    if epoch <= 0 or epoch > 253402300799:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def iso_from_string(value):
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def short_hash(value, length=12):
    value = str(value or "")
    return value[:length] + "…" if len(value) > length else value


def address_key(chain, address):
    """Stable id for one watched address across config and state."""
    lowered = chain in ("eth", "sol", "lightning")
    return f"{chain}:{address.strip().lower() if lowered else address.strip()}"


def clean_label(value, fallback=""):
    if value is None or value == "":
        return fallback
    return text(value, "label", limit=80)


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = item.get("id") if isinstance(item, dict) else item
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# -- address validation -----------------------------------------------------
#
# These are sanity checks against a typo being sent to a provider, not a
# replacement for the provider's own validation. Full checksum validation
# would mean vendoring base58/bech32/EIP-55 implementations; the read-only
# call that follows is authoritative and returns a clear error.

_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")
_BECH32 = re.compile(r"^(bc1|ltc1|tb1)[02-9ac-hj-np-z]{6,87}$")
_ETH = re.compile(r"^0x[0-9a-fA-F]{40}$")
_XMR = re.compile(r"^[48][1-9A-HJ-NP-Za-km-z]{94}$")
_VIEW_KEY = re.compile(r"^[0-9a-fA-F]{64}$")
_LIGHTNING_LOCAL = re.compile(r"^[a-z0-9._+-]{1,64}$")
LIGHTNING_DOMAIN = "blink.sv"
_XPUB = re.compile(r"^(xpub|ypub|zpub|tpub|upub|vpub)[1-9A-HJ-NP-Za-km-z]{100,115}$")
_PRIVATE_PREFIXES = ("xprv", "yprv", "zprv", "tprv", "uprv", "vprv")
_DESCRIPTOR_PREFIX = re.compile(r"^(pkh|wpkh|sh\(wpkh|tr)\(")
_DESCRIPTOR_CHARS = re.compile(r"^[A-Za-z0-9()\[\]{}<>;,:/*'#@._+-]+$")


def validate_lightning_address(value):
    """A Blink Lightning address, canonicalised to name@blink.sv.

    The address is only an identity for the watched account: the API key
    decides which wallet is read. A bare username is accepted and expanded.
    """
    value = text(value, "Lightning address", limit=200)
    if "@" in value:
        local, _, domain = value.rpartition("@")
        if domain.lower() != LIGHTNING_DOMAIN:
            raise ValidationError(f"a Blink Lightning address ends in @{LIGHTNING_DOMAIN}")
    else:
        local = value
    local = local.strip().lower()
    if not _LIGHTNING_LOCAL.match(local) or local[0] in ".-+" or local[-1] in ".-+":
        raise ValidationError("not a Blink Lightning address (name@blink.sv)")
    return f"{local}@{LIGHTNING_DOMAIN}"


def validate_wallet_key(value):
    """An account xpub or output descriptor for a whole Bitcoin wallet.

    A private key is refused by name: a seed or xprv can spend, and pasting
    one here would put it in the config for no reason. The xpub cannot spend,
    but it does reveal every address of the wallet to the provider.
    """
    value = text(value, "wallet key", limit=400)
    if any(ch.isspace() for ch in value):
        raise ValidationError("the wallet key must not contain spaces")
    if _XPUB.match(value):
        return value
    lowered = value.lower()
    if any(prefix in lowered for prefix in _PRIVATE_PREFIXES):
        raise ValidationError(
            "that is a private key; Blockwatch needs the public xpub, never a private key")
    if not _DESCRIPTOR_PREFIX.match(lowered):
        raise ValidationError(
            "not a Bitcoin xpub or output descriptor (xpub, ypub, zpub, wpkh(...), tr(...))")
    if len(value) > 400:
        raise ValidationError("the output descriptor is too long")
    if not _DESCRIPTOR_CHARS.match(value):
        raise ValidationError("the output descriptor contains unexpected characters")
    if value.count("(") != value.count(")"):
        raise ValidationError("the output descriptor has unbalanced parentheses")
    return value


def validate_address(chain, address):
    address = text(address, "address", limit=400 if chain == "btcwallet" else 200)
    if chain == "btc":
        if _BECH32.match(address.lower()):
            return address
        if 26 <= len(address) <= 35 and _BASE58.match(address):
            return address
        raise ValidationError("not a Bitcoin address")
    if chain == "ltc":
        if _BECH32.match(address.lower()):
            return address
        if 26 <= len(address) <= 35 and _BASE58.match(address):
            return address
        raise ValidationError("not a Litecoin address")
    if chain == "doge":
        if 26 <= len(address) <= 35 and _BASE58.match(address) and address[0] in "9AD":
            return address
        raise ValidationError("not a Dogecoin address")
    if chain == "eth":
        if _ETH.match(address):
            return address
        raise ValidationError("not an Ethereum address (0x followed by 40 hex characters)")
    if chain == "sol":
        if 32 <= len(address) <= 44 and _BASE58.match(address):
            return address
        raise ValidationError("not a Solana address")
    if chain == "xmr":
        if _XMR.match(address):
            return address
        raise ValidationError("not a Monero mainnet address")
    if chain == "lightning":
        return validate_lightning_address(address)
    if chain == "btcwallet":
        return validate_wallet_key(address)
    raise ValidationError(f"unsupported chain {chain}")


def validate_view_key(value):
    value = text(value, "view key", limit=64)
    if not _VIEW_KEY.match(value):
        raise ValidationError("the Monero private view key must be 64 hex characters")
    return value.lower()


def validate_label(value):
    return clean_label(value)
