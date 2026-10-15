"""PIN hashing; call these functions in the Home Assistant executor."""

import hashlib
import hmac
import re
import secrets


def valid_pin(pin: object) -> bool:
    """Accept 4–12 ASCII letters or digits, preserving case and leading zeroes."""
    return isinstance(pin, str) and re.fullmatch(r"[A-Za-z0-9]{4,12}", pin) is not None


def hash_pin(pin: str) -> dict[str, str]:
    """Create a salted scrypt hash; never store the original PIN."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return {"salt": salt.hex(), "hash": digest.hex()}


def verify_pin(pin: str, record: dict[str, str]) -> bool:
    """Verify a PIN without a timing-sensitive digest comparison."""
    digest = hashlib.scrypt(
        pin.encode(), salt=bytes.fromhex(record["salt"]), n=16384, r=8, p=1, dklen=32
    )
    return hmac.compare_digest(digest.hex(), record["hash"])
