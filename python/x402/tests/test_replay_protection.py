"""Tests for x402 replay protection."""

import time

from x402.replay_protection import (
    InMemoryReplayProtectionStore,
    extract_nonce,
)


class TestInMemoryReplayProtectionStore:
    """Test the in-memory replay protection store."""

    def test_first_use_returns_false(self):
        store = InMemoryReplayProtectionStore()
        assert store.is_duplicate("nonce-1") is False

    def test_second_use_returns_true(self):
        store = InMemoryReplayProtectionStore()
        store.is_duplicate("nonce-1")
        assert store.is_duplicate("nonce-1") is True

    def test_different_nonces_both_pass(self):
        store = InMemoryReplayProtectionStore()
        assert store.is_duplicate("nonce-1") is False
        assert store.is_duplicate("nonce-2") is False

    def test_same_nonce_different_stores_pass(self):
        store1 = InMemoryReplayProtectionStore()
        store2 = InMemoryReplayProtectionStore()
        store1.is_duplicate("nonce-1")
        # Different store — not a replay
        assert store2.is_duplicate("nonce-1") is False

    def test_stats_tracks_active_nonces(self):
        store = InMemoryReplayProtectionStore()
        stats = store.stats()
        assert stats["active_nonces"] == 0

        store.is_duplicate("nonce-1")
        stats = store.stats()
        assert stats["active_nonces"] == 1

        store.is_duplicate("nonce-2")
        stats = store.stats()
        assert stats["active_nonces"] == 2

    def test_stats_includes_ttl(self):
        store = InMemoryReplayProtectionStore(ttl=600)
        stats = store.stats()
        assert stats["ttl_seconds"] == 600

    def test_ttl_expiry_allows_reuse(self):
        store = InMemoryReplayProtectionStore(ttl=0.1)  # 100ms
        store.is_duplicate("nonce-1")
        time.sleep(0.15)
        # Nonce expired — should pass again
        assert store.is_duplicate("nonce-1") is False

    def test_prune_removes_expired(self):
        store = InMemoryReplayProtectionStore(ttl=0.1)
        store.is_duplicate("nonce-1")
        store.is_duplicate("nonce-2")
        assert store.stats()["active_nonces"] == 2

        time.sleep(0.15)
        # Trigger prune via stats
        stats = store.stats()
        assert stats["active_nonces"] == 0

    def test_store_type_in_stats(self):
        store = InMemoryReplayProtectionStore()
        assert store.stats()["store"] == "in_memory"


class TestExtractNonce:
    """Test nonce extraction from payment payloads."""

    def test_eip3009_nonce(self):
        payload = {
            "authorization": {
                "nonce": "0xf3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480"
            }
        }
        nonce = extract_nonce(payload)
        assert nonce == "0xf3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480"

    def test_permit2_nonce(self):
        payload = {
            "permit2Authorization": {
                "nonce": "33247007178036348590600198031289925668252061821958005840077069883511451257277"
            }
        }
        nonce = extract_nonce(payload)
        assert nonce == "33247007178036348590600198031289925668252061821958005840077069883511451257277"

    def test_fallback_hash(self):
        payload = {"signature": "0xabc123", "data": "some-value"}
        nonce1 = extract_nonce(payload)
        nonce2 = extract_nonce(payload)
        # Same payload → same hash
        assert nonce1 == nonce2
        # Hash is 16 hex chars
        assert len(nonce1) == 16

    def test_different_payloads_different_hashes(self):
        p1 = {"signature": "0xaaa"}
        p2 = {"signature": "0xbbb"}
        assert extract_nonce(p1) != extract_nonce(p2)

    def test_nonce_normalization(self):
        """Whitespace and case are normalized."""
        payload1 = {"authorization": {"nonce": "  0xABC  "}}
        payload2 = {"authorization": {"nonce": "0xabc"}}
        assert extract_nonce(payload1) == extract_nonce(payload2)

    def test_eip3009_takes_precedence(self):
        """If both authorization and permit2Authorization exist, prefer EIP-3009."""
        payload = {
            "authorization": {"nonce": "0xaaa"},
            "permit2Authorization": {"nonce": "0xbbb"},
        }
        nonce = extract_nonce(payload)
        assert nonce == "0xaaa"


class TestReplayProtectionIntegration:
    """Integration tests combining store + nonce extraction."""

    def test_full_flow_first_request(self):
        store = InMemoryReplayProtectionStore()
        payload = {
            "authorization": {
                "nonce": "0xf3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480"
            }
        }
        nonce = extract_nonce(payload)
        assert store.is_duplicate(nonce) is False

    def test_full_flow_duplicate_rejected(self):
        store = InMemoryReplayProtectionStore()
        payload = {
            "authorization": {
                "nonce": "0xf3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480"
            }
        }
        nonce = extract_nonce(payload)
        store.is_duplicate(nonce)
        assert store.is_duplicate(nonce) is True

    def test_full_flow_different_proofs_pass(self):
        store = InMemoryReplayProtectionStore()
        p1 = {"authorization": {"nonce": "0xaaa"}}
        p2 = {"authorization": {"nonce": "0xbbb"}}

        assert store.is_duplicate(extract_nonce(p1)) is False
        assert store.is_duplicate(extract_nonce(p2)) is False
