"""Pluggable storage interface for receipt attestation.

Defines the ReceiptStorage protocol and provides an in-memory default implementation.
Production deployments should implement persistent storage (database, filesystem, etc.).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReceiptStorage(Protocol):
    """Protocol for receipt storage backends.

    Implementations MUST support async operations for compatibility with
    the x402 async server hooks. Sync implementations can wrap operations
    in call_soon_threadsafe or use synchronous alternatives.
    """

    async def store(self, receipt_id: str, receipt: dict[str, Any]) -> None:
        """Store a receipt by ID.

        Args:
            receipt_id: Unique identifier for the receipt.
            receipt: The receipt data as a serializable dict.
        """
        ...

    async def retrieve(self, receipt_id: str) -> dict[str, Any] | None:
        """Retrieve a receipt by ID.

        Args:
            receipt_id: Unique identifier for the receipt.

        Returns:
            The receipt dict if found, None otherwise.
        """
        ...

    async def list_by_payer(self, payer: str) -> list[dict[str, Any]]:
        """List all receipts for a given payer address.

        Args:
            payer: The payer wallet address.

        Returns:
            List of receipt dicts for that payer.
        """
        ...

    async def list_by_resource(self, resource_url: str) -> list[dict[str, Any]]:
        """List all receipts for a given resource URL.

        Args:
            resource_url: The resource URL.

        Returns:
            List of receipt dicts for that resource.
        """
        ...


class InMemoryReceiptStorage:
    """In-memory receipt storage for development and testing.

    Stores receipts in Python dicts. Not suitable for production
    deployments requiring persistence or concurrency.
    """

    def __init__(self) -> None:
        self._receipts: dict[str, dict[str, Any]] = {}
        self._by_payer: dict[str, list[str]] = defaultdict(list)
        self._by_resource: dict[str, list[str]] = defaultdict(list)

    async def store(self, receipt_id: str, receipt: dict[str, Any]) -> None:
        """Store a receipt in memory.

        Args:
            receipt_id: Unique identifier for the receipt.
            receipt: The receipt data as a dict.
        """
        self._receipts[receipt_id] = receipt
        payer = receipt.get("payer", "")
        resource = receipt.get("resourceUrl", "")
        if payer:
            self._by_payer[payer].append(receipt_id)
        if resource:
            self._by_resource[resource].append(receipt_id)

    async def retrieve(self, receipt_id: str) -> dict[str, Any] | None:
        """Retrieve a receipt by ID from memory.

        Args:
            receipt_id: Unique identifier for the receipt.

        Returns:
            The receipt dict if found, None otherwise.
        """
        return self._receipts.get(receipt_id)

    async def list_by_payer(self, payer: str) -> list[dict[str, Any]]:
        """List all receipts for a payer address.

        Args:
            payer: The payer wallet address.

        Returns:
            List of receipt dicts for that payer.
        """
        ids = self._by_payer.get(payer, [])
        return [self._receipts[rid] for rid in ids if rid in self._receipts]

    async def list_by_resource(self, resource_url: str) -> list[dict[str, Any]]:
        """List all receipts for a resource URL.

        Args:
            resource_url: The resource URL.

        Returns:
            List of receipt dicts for that resource.
        """
        ids = self._by_resource.get(resource_url, [])
        return [self._receipts[rid] for rid in ids if rid in self._receipts]

    def clear(self) -> None:
        """Clear all stored receipts (for testing)."""
        self._receipts.clear()
        self._by_payer.clear()
        self._by_resource.clear()
