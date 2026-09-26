import hashlib
import hmac


def sign(secret: str, purpose: str, call_key: str) -> str:
    return hmac.new(secret.encode(), f"{purpose}:{call_key}".encode(), hashlib.sha256).hexdigest()


def verify(secret: str, purpose: str, call_key: str, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign(secret, purpose, call_key).encode()
    return hmac.compare_digest(expected, signature.encode("utf-8", "replace"))
