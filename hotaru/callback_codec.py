from __future__ import annotations

import base64
import secrets

from goygram import ext


def seal_callback(key: bytes, payload: bytes, *, aad: bytes = b"hotaru-cb") -> str:
    nonce = secrets.token_bytes(12)
    blob = ext.aes_gcm_encrypt(key, nonce, payload, aad)
    return base64.urlsafe_b64encode(nonce + blob).decode("ascii").rstrip("=")


def open_callback(key: bytes, token: str, *, aad: bytes = b"hotaru-cb") -> bytes | None:
    try:
        blob = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        if len(blob) < 28:
            return None
        return ext.aes_gcm_decrypt(key, blob[:12], blob[12:], aad)
    except Exception:
        return None
