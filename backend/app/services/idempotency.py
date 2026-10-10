"""Idempotency-Key handling for the pay operation.

Scope: keys are global across this service's pay operation. A key identifies
one payment of one session; reusing it for any other session is a conflict.
Raw keys are never stored or logged, only their SHA-256 digest.
"""

import hashlib
import uuid

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
# 1-128 characters of unreserved URL characters plus ':'.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._~:-]{1,128}$"


def digest_key(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()


def pay_fingerprint(session_id: uuid.UUID) -> str:
    """Identifies the request a key was first used for: operation + session."""
    return hashlib.sha256(f"pay:{session_id}".encode("ascii")).hexdigest()


def advisory_lock_id(key_digest: str) -> int:
    """First 64 bits of the digest as a signed bigint for pg_advisory_xact_lock.

    Only used to serialize requests; a collision just makes two unrelated keys
    wait for each other. Identity is enforced by the full digest's unique
    constraint.
    """
    return int.from_bytes(bytes.fromhex(key_digest)[:8], "big", signed=True)
