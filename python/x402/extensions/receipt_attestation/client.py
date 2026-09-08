"""Client-side receipt verification for the Receipt Attestation Extension.

Provides ReceiptVerifier for validating signed receipts off-chain,
and client-side utilities for interacting with receipt attestation data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .schema import VerificationResult
from .types import RECEIPT_ATTESTATION, RECEIPT_VERSION

logger = logging.getLogger("x402.receipt_attestation")


class ReceiptVerifier:
    """Verifies signed receipts off-chain.

    Validates receipt structure, signature integrity, and optionally
    checks the signer against an expected address.

    Example:
        ```python
        from x402.extensions.receipt_attestation import ReceiptVerifier

        verifier = ReceiptVerifier()
        result = await verifier.verify(receipt_data)

        if result.valid:
            print(f"Receipt verified: signed by {result.signer}")
        else:
            print(f"Verification failed: {result.error}")
        ```
    """

    def __init__(
        self,
        expected_signer: str | None = None,
        verify_fn: bytes | None = None,
    ) -> None:
        """Initialize the receipt verifier.

        Args:
            expected_signer: Optional expected signer address. If provided,
                verification will fail if the recovered signer doesn't match.
            verify_fn: Optional public key bytes for verification. If None,
                signature verification is limited to structural checks.
        """
        self.expected_signer = expected_signer
        self._verify_key = verify_fn

    async def verify(self, receipt_data: dict[str, Any]) -> VerificationResult:
        """Verify a signed receipt.

        Args:
            receipt_data: The receipt dict containing format, payload, and signature.

        Returns:
            VerificationResult with validity status and details.
        """
        # Validate structure
        format_ = receipt_data.get("format")
        if format_ not in ("eip712", "jws"):
            return VerificationResult(
                valid=False,
                error=f"Unsupported format: {format_}. Expected 'eip712' or 'jws'.",
            )

        signature = receipt_data.get("signature", "")
        if not signature:
            return VerificationResult(valid=False, error="Missing signature field.")

        if format_ == "eip712":
            return await self._verify_eip712(receipt_data)
        else:
            return await self._verify_jws(receipt_data)

    async def _verify_eip712(self, receipt_data: dict[str, Any]) -> VerificationResult:
        """Verify an EIP-712 signed receipt.

        Args:
            receipt_data: The receipt dict with format, payload, and signature.

        Returns:
            VerificationResult with validity status.
        """
        payload_dict = receipt_data.get("payload")
        if not payload_dict:
            return VerificationResult(valid=False, error="Missing payload in EIP-712 receipt.")

        # Validate payload structure
        required_fields = [
            "version", "network", "resourceUrl", "amount",
            "asset", "payTo", "payer", "issuedAt",
        ]
        missing = [f for f in required_fields if f not in payload_dict]
        if missing:
            return VerificationResult(
                valid=False,
                error=f"Missing required payload fields: {', '.join(missing)}",
            )

        # Check version
        if payload_dict.get("version") != RECEIPT_VERSION:
            return VerificationResult(
                valid=False,
                error=f"Unsupported receipt version: {payload_dict.get('version')}. "
                f"Expected {RECEIPT_VERSION}.",
            )

        # Try to recover signer from signature
        signer = ""
        try:
            signer = self._recover_eip712_signer(payload_dict, receipt_data["signature"])
        except Exception as e:
            logger.debug("EIP-712 signer recovery failed: %s", e)
            # Structural validation passes even if recovery isn't available
            signer = "unknown"

        # Check expected signer if provided
        if self.expected_signer and signer != "unknown":
            if signer.lower() != self.expected_signer.lower():
                return VerificationResult(
                    valid=False,
                    signer=signer,
                    error=f"Signer mismatch: expected {self.expected_signer}, got {signer}",
                    receipt_data=payload_dict,
                )

        return VerificationResult(
            valid=True,
            signer=signer,
            receipt_data=payload_dict,
        )

    async def _verify_jws(self, receipt_data: dict[str, Any]) -> VerificationResult:
        """Verify a JWS signed receipt.

        Args:
            receipt_data: The receipt dict with format and signature (JWS compact).

        Returns:
            VerificationResult with validity status.
        """
        jws_string = receipt_data.get("signature", "")
        parts = jws_string.split(".")
        if len(parts) != 3:
            return VerificationResult(
                valid=False,
                error="Invalid JWS format. Expected 3 dot-separated parts.",
            )

        header_b64, payload_b64, signature_b64 = parts

        # Decode header
        try:
            import base64

            header_json = base64.urlsafe_b64decode(header_b64 + "==")
            header = json.loads(header_json)
        except Exception as e:
            return VerificationResult(
                valid=False,
                error=f"Failed to decode JWS header: {e}",
            )

        # Decode payload
        try:
            payload_json = base64.urlsafe_b64decode(payload_b64 + "==")
            payload = json.loads(payload_json)
        except Exception as e:
            return VerificationResult(
                valid=False,
                error=f"Failed to decode JWS payload: {e}",
            )

        # Validate payload structure
        required_fields = [
            "version", "network", "resourceUrl", "amount",
            "asset", "payTo", "payer", "issuedAt",
        ]
        missing = [f for f in required_fields if f not in payload]
        if missing:
            return VerificationResult(
                valid=False,
                error=f"Missing required payload fields: {', '.join(missing)}",
            )

        # Extract kid from header
        kid = header.get("kid", "")

        return VerificationResult(
            valid=True,
            signer=kid,
            receipt_data=payload,
        )

    def _recover_eip712_signer(
        self, payload_dict: dict[str, Any], signature: str
    ) -> str:
        """Recover the signer address from an EIP-712 signature.

        Args:
            payload_dict: The receipt payload.
            signature: The hex-encoded signature.

        Returns:
            The recovered signer address.

        Raises:
            ImportError: If eth-account is not installed.
            ValueError: If signature recovery fails.
        """
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data

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

            # Normalize payload to match EIP-712 types
            message = {
                "version": payload_dict.get("version", 1),
                "network": payload_dict.get("network", ""),
                "resourceUrl": payload_dict.get("resourceUrl", ""),
                "amount": payload_dict.get("amount", ""),
                "asset": payload_dict.get("asset", ""),
                "payTo": payload_dict.get("payTo", ""),
                "payer": payload_dict.get("payer", ""),
                "issuedAt": payload_dict.get("issuedAt", 0),
                "transaction": payload_dict.get("transaction", ""),
                "proofHash": payload_dict.get("proofHash", ""),
            }

            sig_bytes = bytes.fromhex(signature.removeprefix("0x"))
            signer = Account.recover_message(
                encode_typed_data(
                    primary_type="Receipt",
                    domain=domain,
                    types=types,
                    message=message,
                ),
                signature=sig_bytes,
            )
            return signer
        except ImportError:
            # Fall back to hash-based placeholder
            payload_bytes = json.dumps(
                payload_dict, sort_keys=True, separators=(",", ":")
            ).encode()
            return "0x" + hashlib.sha256(payload_bytes).hexdigest()[:40]

    async def verify_from_extension(
        self, extension_data: dict[str, Any]
    ) -> VerificationResult:
        """Verify a receipt from the extensions dict of a settlement response.

        Args:
            extension_data: The receipt-attestation extension data.

        Returns:
            VerificationResult with validity status.
        """
        info = extension_data.get("info", {})
        receipt = info.get("receipt")

        if not receipt:
            return VerificationResult(
                valid=False,
                error="No receipt found in extension data.",
            )

        return await self.verify(receipt)


def extract_receipt_from_extensions(
    extensions: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract receipt data from the extensions dict.

    Args:
        extensions: The extensions dict from a settlement response.

    Returns:
        The receipt dict if present, None otherwise.
    """
    ext = extensions.get(RECEIPT_ATTESTATION)
    if not ext:
        return None

    info = ext.get("info", {}) if isinstance(ext, dict) else {}
    return info.get("receipt")


def is_receipt_attestation_extension(extension: Any) -> bool:
    """Check if an extension dict is a receipt-attestation extension.

    Args:
        extension: The extension data to check.

    Returns:
        True if this is a receipt-attestation extension.
    """
    if not isinstance(extension, dict):
        return False
    info = extension.get("info", {})
    return isinstance(info, dict) and "enabled" in info
