# Extension: `replay-protection`

## Summary

The `replay-protection` extension adds server-side enforcement to prevent payment proof replay attacks during the HTTP-layer TOCTOU window — the time between payment verification and blockchain settlement.

While on-chain nonces (EIP-3009, Permit2) prevent replay **after** settlement, and the `payment-identifier` extension provides client-side idempotency keys, neither prevents a malicious client from submitting the same valid payment proof to multiple endpoints or servers before settlement completes.

This extension provides a server-side mechanism that:
1. Extracts a deterministic nonce from the payment proof
2. Tracks seen nonces in a server-side store
3. Rejects duplicate proofs with `409 Conflict` before any processing

---

## Motivation

### The TOCTOU Window

The x402 payment flow has a window between verification and settlement:

```
Client → Resource Server: PAYMENT-SIGNATURE header
Resource Server → Facilitator: /verify
Facilitator → Resource Server: valid
                              ← TOCTOU WINDOW →
Resource Server → Facilitator: /settle
Facilitator → Blockchain: broadcast
```

During this window (typically 100-2000ms), the same payment proof could be:
- Submitted to the same server multiple times (replay)
- Submitted to different servers (parallel spend attempt)
- Intercepted and replayed by a network attacker

On-chain nonces prevent double-spending **after** settlement, but during the TOCTOU window, the same proof can trigger multiple verify/settle cycles, wasting gas and potentially causing confusion.

### Why `payment-identifier` Is Not Sufficient

The existing `payment-identifier` extension is:
- **Optional** — servers cannot enforce it without client cooperation
- **Client-generated** — a malicious client can send different identifiers for the same proof
- **Response-caching focused** — designed for idempotency, not security enforcement

### What `replay-protection` Adds

- **Mandatory server-side enforcement** — the server tracks seen nonces regardless of client behavior
- **Proof-derived nonces** — extracted from the cryptographic proof itself, not client-provided
- **Instant rejection** — `409 Conflict` before facilitator call, saving gas and processing

---

## Specification

### Nonce Extraction

The nonce is extracted deterministically from the payment proof:

| Scheme | Nonce Source | Derivation |
|--------|-------------|------------|
| `exact` (EIP-3009) | `payload.authorization.nonce` | Direct use (32-byte hex) |
| `exact` (Permit2) | `payload.permit2Authorization.nonce` | Direct use (uint256) |
| `exact` (ERC-7710) | `payload.permissionContext` | SHA-256 hash (opaque bytes) |
| `upto` | `payload.authorization.nonce` | Direct use (32-byte hex) |
| `batch-settlement` | Voucher signature | SHA-256 of signed voucher |

For schemes where the nonce is not directly available (or is opaque), the server computes:
```
nonce_hash = SHA-256(normalize(payload))[:16]
```

Where `normalize()` strips whitespace and lowercases hex strings.

### Server-Side Store

Servers maintain a `ReplayProtectionStore`:

```python
class ReplayProtectionStore(Protocol):
    """Thread-safe store for tracking seen payment nonces."""

    def is_duplicate(self, nonce: str) -> bool:
        """Check if nonce was already seen. If not, record it and return False."""
        ...

    def stats(self) -> dict:
        """Return store stats for monitoring."""
        ...
```

The store must be:
- **Thread-safe** — safe for concurrent HTTP requests
- **TTL-based** — entries expire after a configurable period (default: 300 seconds)
- **Per-process** for single-instance servers, or **shared** (e.g., Redis) for multi-instance

### Extension Registration

Servers advertise support in `PaymentRequired`:

```json
{
  "extensions": {
    "replay-protection": {
      "info": {
        "required": true,
        "ttl_seconds": 300
      }
    }
  }
}
```

### Client Behavior

Clients do NOT need to send anything special for replay-protection. The server extracts nonces from the existing `PaymentPayload` fields.

However, clients that also use `payment-identifier` benefit from:
- Client-side idempotency (same proof, same id → cached response)
- Server-side enforcement (same proof, any id → `409 Conflict`)

### Response Codes

| Scenario | Response |
|----------|----------|
| New nonce | Process normally |
| Duplicate nonce (same endpoint) | `409 Conflict` |
| Duplicate nonce (different endpoint) | `409 Conflict` |
| Duplicate nonce after TTL expiry | Process normally (nonce evicted) |

### `409 Conflict` Response Body

```json
{
  "error": "Payment proof already used",
  "detail": "Each payment proof can only be used once. This proof was previously submitted.",
  "nonce": "e99a18c428cb38d5f260853678922e03",
  "hint": "Generate a new payment proof for this request."
}
```

---

## Integration with Existing Components

### Relationship to `PendingSettlementStore`

The `PendingSettlementStore` tracks **broadcast-but-not-confirmed** transactions for retry. The `ReplayProtectionStore` tracks **verified-but-not-settled** proofs for replay prevention. They serve different purposes:

| Store | Purpose | Key | TTL |
|-------|---------|-----|-----|
| `PendingSettlementStore` | Retry failed settlements | tx_hash | 300s |
| `ReplayProtectionStore` | Block replay attacks | proof_nonce | 300s |

### Relationship to `payment-identifier`

| Feature | `payment-identifier` | `replay-protection` |
|---------|---------------------|---------------------|
| Direction | Client → Server | Server-side only |
| Enforcement | Optional | Mandatory |
| Nonce source | Client-generated | Proof-derived |
| Purpose | Idempotency | Security |
| Can coexist | Yes | Yes |

---

## Implementation Guide

### Python SDK

Add to `python/x402/replay_protection.py`:

```python
import hashlib
import threading
import time

class InMemoryReplayProtectionStore:
    """Default replay protection store (single-instance)."""

    def __init__(self, ttl: int = 300):
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def is_duplicate(self, nonce: str) -> bool:
        with self._lock:
            self._prune()
            if nonce in self._seen:
                return True
            self._seen[nonce] = time.monotonic()
            return False

    def stats(self) -> dict:
        with self._lock:
            self._prune()
            return {"active_nonces": len(self._seen), "ttl_seconds": self._ttl}

    def _prune(self):
        cutoff = time.monotonic() - self._ttl
        expired = [k for k, v in self._seen.items() if v < cutoff]
        for k in expired:
            del self._seen[k]
```

### Integration Point

In `x402ResourceServer.verify_payment()`, before calling the facilitator:

```python
# Check replay protection
if self._replay_store and self._replay_store.is_duplicate(extracted_nonce):
    return ResourceVerifyResponse(
        is_valid=False,
        invalid_reason="Payment proof already used",
    )
```

---

## Security Analysis

### What This Prevents

1. **HTTP-layer replay** — same proof submitted to same server multiple times
2. **Parallel spend** — same proof submitted to different endpoints before settlement
3. **Gas waste** — facilitator doesn't waste gas verifying already-seen proofs

### What This Does NOT Prevent

1. **Post-settlement replay** — on-chain nonces handle this
2. **Client-side proof sharing** — if a client shares their proof, others can use it once (until first settlement)
3. **Sybil attacks** — multiple clients creating multiple proofs (requires rate limiting)

### Trust Assumptions

- The server is trusted to maintain the store honestly
- The store is per-process (or shared via Redis for multi-instance)
- TTL-based expiry is acceptable (not permanent deduplication)

---

## Test Vectors

### Test 1: First Request Passes

```json
// Request 1
POST /v1/premium-data
X-PAYMENT: {"scheme":"exact","network":"eip155:8453",...}

// Response: 200 OK
```

### Test 2: Duplicate Rejected

```json
// Request 1 (same proof)
POST /v1/premium-data
X-PAYMENT: {"scheme":"exact","network":"eip155:8453",...}

// Response: 200 OK

// Request 2 (identical proof)
POST /v1/premium-data
X-PAYMENT: {"scheme":"exact","network":"eip155:8453",...}

// Response: 409 Conflict
```

### Test 3: Different Proofs Both Pass

```json
// Request 1 (proof A)
POST /v1/premium-data
X-PAYMENT: {"scheme":"exact","network":"eip155:8453","payload":{"authorization":{"nonce":"0xaaa..."},...}}

// Response: 200 OK

// Request 2 (proof B)
POST /v1/premium-data
X-PAYMENT: {"scheme":"exact","network":"eip155:8453","payload":{"authorization":{"nonce":"0xbbb..."},...}}

// Response: 200 OK
```

### Test 4: TTL Expiry

```json
// Request 1
POST /v1/premium-data
X-PAYMENT: {...}

// Response: 200 OK

// Wait 301 seconds (TTL = 300s)

// Request 2 (same proof)
POST /v1/premium-data
X-PAYMENT: {...}

// Response: 200 OK (nonce evicted)
```
