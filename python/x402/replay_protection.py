"""Replay protection store for x402 resource servers.

Provides server-side enforcement to prevent payment proof replay attacks
during the HTTP-layer TOCTOU window (between verification and settlement).

See specs/extensions/replay-protection.md for the full specification.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Protocol, runtime_checkable

# Default TTL for nonce entries (5 minutes).
# Matches PendingSettlementStore TTL — both cover the verify→settle window.
REPLAY_PROTECTION_TTL_SECONDS = 300.0


@runtime_checkable
class ReplayProtectionStore(Protocol):
    """Protocol for a replay protection store.

    Implementations must be safe for concurrent use.
    """

    def is_duplicate(self, nonce: str) -> bool:
        """Check if nonce was already seen. If not, record it and return False.

        Returns True when the nonce is a replay (already seen and not expired).
        """
        ...

    def stats(self) -> dict:
        """Return store stats for monitoring."""
        ...


class InMemoryReplayProtectionStore:
    """Default ReplayProtectionStore implementation.

    A lock-protected, per-process dict with lazy TTL pruning.
    Never performs network I/O — suitable for single-instance resource servers.
    Multi-instance deployments should inject a shared, network-backed
    ReplayProtectionStore implementation (e.g. Redis).
    """

    def __init__(self, ttl: float = REPLAY_PROTECTION_TTL_SECONDS) -> None:
        self._seen: dict[str, float] = {}  # nonce -> monotonic timestamp
        self._lock = threading.Lock()
        self._ttl = ttl

    def is_duplicate(self, nonce: str) -> bool:
        with self._lock:
            self._prune()
            if nonce in self._seen:
                return True  # REPLAY DETECTED
            self._seen[nonce] = time.monotonic()
            return False

    def stats(self) -> dict:
        with self._lock:
            self._prune()
            return {
                "active_nonces": len(self._seen),
                "ttl_seconds": self._ttl,
                "store": "in_memory",
            }

    def _prune(self) -> None:
        """Remove entries older than TTL. Caller must hold lock."""
        cutoff = time.monotonic() - self._ttl
        expired = [k for k, v in self._seen.items() if v < cutoff]
        for k in expired:
            del self._seen[k]


def extract_nonce(payload: dict) -> str:
    """Extract a deterministic nonce from a payment payload.

    The nonce is used as the replay protection key. For schemes where
    the authorization nonce is directly available (EIP-3009, Permit2),
    it is used directly. For opaque payloads, a SHA-256 hash is used.

    Args:
        payload: The payment payload dict (from PAYMENT-SIGNATURE header).

    Returns:
        A string nonce suitable for replay detection.
    """
    # Try EIP-3009 authorization nonce
    if "authorization" in payload:
        auth = payload["authorization"]
        if isinstance(auth, dict) and "nonce" in auth:
            return str(auth["nonce"]).strip().lower()

    # Try Permit2 authorization nonce
    if "permit2Authorization" in payload:
        p2 = payload["permit2Authorization"]
        if isinstance(p2, dict) and "nonce" in p2:
            return str(p2["nonce"]).strip().lower()

    # Fallback: hash the entire payload
    import json
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "REPLAY_PROTECTION_TTL_SECONDS",
    "ReplayProtectionStore",
    "InMemoryReplayProtectionStore",
    "extract_nonce",
]
