"""Type definitions for the Receipt Attestation Extension.

Provides data models for cryptographic receipts proving payment was made,
enabling off-chain verification, portable proof, and audit trails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

# Extension identifier constant
RECEIPT_ATTESTATION = "receipt-attestation"

# Current receipt schema version
RECEIPT_VERSION = 1


@dataclass
class ReceiptData:
    """Core receipt data produced after successful settlement.

    Attributes:
        version: Receipt schema version.
        network: Blockchain network in CAIP-2 format (e.g., "eip155:8453").
        resource_url: The paid resource URL.
        amount: Payment amount in smallest unit.
        asset: Token contract address or "native".
        pay_to: Recipient wallet address.
        payer: Payer wallet address.
        issued_at: Unix timestamp (seconds) when receipt was issued.
        transaction: Optional blockchain transaction hash.
        proof_hash: Optional hash of the payment proof for additional integrity.
    """

    version: int = RECEIPT_VERSION
    network: str = ""
    resource_url: str = ""
    amount: str = ""
    asset: str = ""
    pay_to: str = ""
    payer: str = ""
    issued_at: int = 0
    transaction: str = ""
    proof_hash: str = ""


@dataclass
class AttestationData:
    """Extended attestation wrapping a signed receipt with server metadata.

    Attributes:
        receipt: The signed receipt as a dict (EIP-712 or JWS format).
        attester: DID or address of the attesting server.
        issued_at: Timestamp of the attestation.
        expires_at: Optional expiration timestamp.
        metadata: Arbitrary key-value metadata.
    """

    receipt: dict[str, Any] = field(default_factory=dict)
    attester: str = ""
    issued_at: int = 0
    expires_at: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class ReceiptInfo(BaseModel):
    """Pydantic model for receipt info in the extension declaration.

    Attributes:
        enabled: Whether the server will generate receipts.
        supported_formats: List of supported signing formats ("eip712", "jws").
    """

    enabled: bool = True
    supported_formats: list[str] = Field(default_factory=lambda: ["eip712"])

    model_config = {"extra": "allow"}


class ReceiptAttestationExtension(BaseModel):
    """Pydantic model for the full receipt-attestation extension payload.

    Attributes:
        info: The receipt info.
        schema_: JSON Schema validating the info structure.
    """

    info: ReceiptInfo
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"extra": "allow", "populate_by_name": True}


class VerificationResult(BaseModel):
    """Result of receipt verification.

    Attributes:
        valid: Whether the receipt is cryptographically valid.
        signer: The recovered signer address (if EIP-712) or kid (if JWS).
        error: Error message if verification failed.
        receipt_data: The decoded receipt payload if valid.
    """

    valid: bool = False
    signer: str = ""
    error: str = ""
    receipt_data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}
