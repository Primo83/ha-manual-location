"""PIN hashing; call these functions in the Home Assistant executor."""

import hashlib
import hmac
import re
import secrets


def valid_pin(pin: object) -> bool:
    """Accept an explicit 6–12 digit ASCII PIN, including leading zeroes."""
    return isinstance(pin, str) and re.fullmatch(r"[0-9]{6,12}", pin) is not None


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
