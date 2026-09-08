"""Server-side receipt generation for the Receipt Attestation Extension.

Provides ReceiptGenerator for creating signed receipts after settlement,
and a ResourceServerExtension for integrating with x402ResourceServer hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from .schema import ReceiptPayloadModel, SignedReceiptModel, receipt_attestation_schema
from .storage import InMemoryReceiptStorage, ReceiptStorage
from .types import RECEIPT_ATTESTATION, RECEIPT_VERSION

logger = logging.getLogger("x402.receipt_attestation")


def _default_id_generator(payload: ReceiptPayloadModel) -> str:
    """Generate a receipt ID from payload content hash."""
    content = f"{payload.network}:{payload.resource_url}:{payload.payer}:{payload.issued_at}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class ReceiptGenerator:
    """Generates signed receipts after successful settlement.

    Integrates with x402ResourceServer via the on_after_settle hook to
    automatically produce cryptographic receipts when payments settle.

    Attributes:
        signing_key: The private key for signing (bytes or hex string).
            For EIP-712, this is an ECDSA secp256k1 private key.
        storage: Storage backend for persisting receipts.
        format_: Signing format ("eip712" or "jws").
        attester: Optional DID or address of the attesting server.
        sign_fn: Optional custom signing function. If None, a default
            signing implementation is used.
        id_generator: Optional custom receipt ID generator.
        include_transaction: Whether to include transaction hash in receipts.
        include_proof_hash: Whether to include proof hash in receipts.
    """

    def __init__(
        self,
        signing_key: bytes | str | None = None,
        storage: ReceiptStorage | None = None,
        format_: str = "eip712",
        attester: str = "",
        sign_fn: Callable[[bytes], Coroutine[Any, Any, str]] | None = None,
        id_generator: Callable[[ReceiptPayloadModel], str] | None = None,
        include_transaction: bool = True,
        include_proof_hash: bool = False,
    ) -> None:
        self.signing_key = signing_key
        self.storage = storage or InMemoryReceiptStorage()
        self.format = format_
        self.attester = attester
        self._sign_fn = sign_fn
        self._id_generator = id_generator or _default_id_generator
        self.include_transaction = include_transaction
        self.include_proof_hash = include_proof_hash

    async def generate(
        self,
        network: str,
        resource_url: str,
        amount: str,
        asset: str,
        pay_to: str,
        payer: str,
        transaction: str = "",
        proof_hash: str = "",
    ) -> dict[str, Any]:
        """Generate a signed receipt for a completed payment.

        Args:
            network: CAIP-2 network identifier (e.g., "eip155:8453").
            resource_url: The paid resource URL.
            amount: Payment amount in smallest unit.
            asset: Token contract address or "native".
            pay_to: Recipient wallet address.
            payer: Payer wallet address.
            transaction: Optional blockchain transaction hash.
            proof_hash: Optional payment proof hash.

        Returns:
            The signed receipt as a dict ready for extension payload.
        """
        issued_at = int(time.time())

        # Build receipt payload
        payload = ReceiptPayloadModel(
            version=RECEIPT_VERSION,
            network=network,
            resource_url=resource_url,
            amount=amount,
            asset=asset,
            pay_to=pay_to,
            payer=payer,
            issued_at=issued_at,
            transaction=transaction if self.include_transaction else "",
            proof_hash=proof_hash if self.include_proof_hash else "",
        )

        # Generate receipt ID
        receipt_id = self._id_generator(payload)

        # Sign the payload
        signature = await self._sign_payload(payload)

        # Build signed receipt
        signed_receipt = SignedReceiptModel(
            format=self.format,
            payload=payload if self.format == "eip712" else None,
            signature=signature,
        )

        # Store the receipt
        receipt_dict = {
            "id": receipt_id,
            **signed_receipt.model_dump(exclude_none=True, by_alias=True),
        }
        await self.storage.store(receipt_id, receipt_dict)

        logger.info("Generated receipt %s for payer %s", receipt_id, payer)

        return receipt_dict

    async def generate_from_settle_context(
        self,
        settle_context: Any,
        requirements: Any,
    ) -> dict[str, Any] | None:
        """Generate a receipt from a settlement context.

        This is the hook adapter for x402ResourceServer.on_after_settle().

        Args:
            settle_context: The SettleResultContext from the server hook.
            requirements: The PaymentRequirements that were settled.

        Returns:
            The signed receipt dict, or None if settlement failed.
        """
        if not hasattr(settle_context, "settle_response"):
            return None

        settle_response = settle_context.settle_response
        if not hasattr(settle_response, "success") or not settle_response.success:
            return None

        # Extract fields from settle context
        network = getattr(requirements, "network", "")
        resource_url = getattr(requirements, "resource", "")
        if hasattr(requirements, "resource_url"):
            resource_url = requirements.resource_url

        amount = getattr(requirements, "amount", "0")
        asset = getattr(requirements, "asset", "")
        pay_to = getattr(requirements, "payTo", "")

        # Extract payer and transaction from settle response or payload
        payer = ""
        transaction = ""
        if hasattr(settle_context, "payload"):
            payload = settle_context.payload
            payer = getattr(payload, "from", "") or getattr(payload, "payer", "")
            if hasattr(payload, "raw"):
                raw = payload.raw
                if isinstance(raw, dict):
                    payer = raw.get("from", payer)

        if hasattr(settle_response, "transaction"):
            transaction = settle_response.transaction or ""

        if not payer:
            logger.warning("No payer found in settle context, skipping receipt")
            return None

        return await self.generate(
            network=network,
            resource_url=resource_url,
            amount=amount,
            asset=asset,
            pay_to=pay_to,
            payer=payer,
            transaction=transaction,
        )

    async def _sign_payload(self, payload: ReceiptPayloadModel) -> str:
        """Sign the receipt payload.

        Args:
            payload: The receipt payload to sign.

        Returns:
            Hex-encoded signature string.
        """
        if self._sign_fn:
            # Use custom signing function
            payload_bytes = json.dumps(
                payload.to_eip712_dict(), sort_keys=True, separators=(",", ":")
            ).encode()
            return await self._sign_fn(payload_bytes)

        if self.format == "eip712":
            return self._sign_eip712(payload)
        elif self.format == "jws":
            return self._sign_jws(payload)
        else:
            raise ValueError(f"Unsupported signing format: {self.format}")

    def _sign_eip712(self, payload: ReceiptPayloadModel) -> str:
        """Sign using EIP-712 (placeholder — requires web3/eth-account)."""
        if self.signing_key is None:
            # Return a deterministic placeholder for testing
            payload_bytes = json.dumps(
                payload.to_eip712_dict(), sort_keys=True, separators=(",", ":")
            ).encode()
            return "0x" + hashlib.sha256(payload_bytes).hexdigest()

        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data

            # Build EIP-712 typed data
            domain = {"name": "x402 receipt", "version": "1", "chainId": 1}
            types = {
                "Receipt": [
                    {"name": "version", "type": "uint256"},
                    {"name": "network", "type": "string"},
                    {"name": "resourceUrl", "type": "string"},
                    {"name": "amount", "type": "string"},
                    {"name": "asset", "type": "string"},
                    {"name": "payTo", "type": "string"},
                    {"name": "payer", "type": "string"},
                    {"name": "issuedAt", "type": "uint256"},
                    {"name": "transaction", "type": "string"},
                    {"name": "proofHash", "type": "string"},
                ]
            }

            message = {
                "version": payload.version,
                "network": payload.network,
                "resourceUrl": payload.resource_url,
                "amount": payload.amount,
                "asset": payload.asset,
                "payTo": payload.pay_to,
                "payer": payload.payer,
                "issuedAt": payload.issued_at,
                "transaction": payload.transaction or "",
                "proofHash": payload.proof_hash or "",
            }

            signer = Account.from_key(self.signing_key)
            signed = signer.sign_message(
                encode_typed_data(
                    primary_type="Receipt",
                    domain=domain,
                    types=types,
                    message=message,
                )
            )
            return "0x" + signed.signature.hex()
        except ImportError:
            logger.warning("eth-account not installed, using placeholder signature")
            payload_bytes = json.dumps(
                payload.to_eip712_dict(), sort_keys=True, separators=(",", ":")
            ).encode()
            return "0x" + hashlib.sha256(payload_bytes).hexdigest()

    def _sign_jws(self, payload: ReceiptPayloadModel) -> str:
        """Sign using JWS (placeholder — requires jose or PyJWT)."""
        payload_bytes = json.dumps(
            payload.to_eip712_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        # Placeholder: base64url(payload).base64url(hash)
        import base64

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "ES256K", "kid": "did:web:example.com#key-1"}).encode()
        ).rstrip(b"=")
        body = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=")
        sig = base64.urlsafe_b64encode(
            hashlib.sha256(header + b"." + body).digest()
        ).rstrip(b"=")
        return f"{header.decode()}.{body.decode()}.{sig.decode()}"


def receipt_attestation_server_extension(
    generator: ReceiptGenerator,
) -> ReceiptAttestationResourceServerExtension:
    """Create a ReceiptAttestationResourceServerExtension instance.

    Args:
        generator: The ReceiptGenerator to use for receipt creation.

    Returns:
        A configured extension instance.
    """
    return ReceiptAttestationResourceServerExtension(generator)


class ReceiptAttestationResourceServerExtension:
    """ResourceServerExtension for receipt-attestation.

    Integrates ReceiptGenerator with x402ResourceServer hooks to
    automatically generate signed receipts after settlement.
    """

    def __init__(self, generator: ReceiptGenerator) -> None:
        self._generator = generator

    @property
    def key(self) -> str:
        """Extension key."""
        return RECEIPT_ATTESTATION

    def enrich_declaration(
        self,
        declaration: Any,
        transport_context: Any,
    ) -> Any:
        """Enrich extension declaration with receipt-attestation info.

        Args:
            declaration: The extension declaration to enrich.
            transport_context: Framework-specific context.

        Returns:
            Enriched declaration with receipt info.
        """
        if not isinstance(declaration, dict):
            declaration = {}

        declaration.setdefault("info", {})
        declaration["info"]["enabled"] = True
        declaration["info"]["supportedFormats"] = [self._generator.format]
        declaration["schema"] = receipt_attestation_schema

        return declaration

    def enrich_settlement_response(
        self,
        declaration: Any,
        settle_result_context: Any,
    ) -> Any | None:
        """Enrich settle response with the generated receipt.

        This is called after settlement completes. The actual receipt
        generation happens in the on_after_settle hook.

        Returns:
            None (receipt is added via the hook, not enrichment).
        """
        return None

    @property
    def generate_receipt(
        self,
    ) -> Callable[[Any, Any], Coroutine[Any, Any, dict[str, Any] | None]]:
        """Return the receipt generation hook function.

        Returns:
            An async callable suitable for x402ResourceServer.on_after_settle().
        """
        return self._on_after_settle

    async def _on_after_settle(self, context: Any) -> None:
        """Hook called after successful settlement to generate receipt.

        Args:
            context: SettleResultContext from x402ResourceServer.
        """
        try:
            requirements = getattr(context, "requirements", None)
            if requirements is None and hasattr(context, "payment_requirements"):
                requirements = context.payment_requirements

            receipt = await self._generator.generate_from_settle_context(
                context, requirements
            )
            if receipt:
                logger.info(
                    "Receipt generated for settlement: %s",
                    receipt.get("id", "unknown"),
                )
        except Exception:
            logger.exception("Failed to generate receipt after settlement")
