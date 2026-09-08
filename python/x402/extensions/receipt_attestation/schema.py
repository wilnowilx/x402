"""JSON Schema and Pydantic models for receipt attestation serialization.

Provides Pydantic models for JSON serialization of receipts and attestations,
and a JSON Schema for validating receipt payloads.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# JSON Schema for validating receipt info in extension declarations.
# Compliant with JSON Schema Draft 2020-12.
receipt_attestation_schema: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "description": "Whether the server generates signed receipts.",
        },
        "supported_formats": {
            "type": "array",
            "items": {"type": "string", "enum": ["eip712", "jws"]},
            "description": "Signing formats supported by this server.",
        },
    },
    "required": ["enabled"],
}

# JSON Schema for validating a receipt payload.
receipt_payload_schema: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "version": {"type": "integer", "const": 1},
        "network": {"type": "string", "description": "CAIP-2 network identifier"},
        "resourceUrl": {"type": "string", "description": "The paid resource URL"},
        "amount": {"type": "string", "description": "Payment amount in smallest unit"},
        "asset": {"type": "string", "description": "Token contract address or native"},
        "payTo": {"type": "string", "description": "Recipient wallet address"},
        "payer": {"type": "string", "description": "Payer wallet address"},
        "issuedAt": {"type": "integer", "description": "Unix timestamp (seconds)"},
        "transaction": {
            "type": "string",
            "description": "Optional blockchain transaction hash",
        },
        "proofHash": {
            "type": "string",
            "description": "Optional payment proof hash",
        },
    },
    "required": [
        "version",
        "network",
        "resourceUrl",
        "amount",
        "asset",
        "payTo",
        "payer",
        "issuedAt",
    ],
}


class ReceiptPayloadModel(BaseModel):
    """Pydantic model for a receipt payload (the signed content).

    Matches the EIP-712 Receipt schema from the offer-receipt extension spec.
    """

    version: int = Field(default=1, description="Receipt schema version")
    network: str = Field(default="", description="CAIP-2 network identifier")
    resource_url: str = Field(
        default="", alias="resourceUrl", description="The paid resource URL"
    )
    amount: str = Field(default="", description="Payment amount in smallest unit")
    asset: str = Field(default="", description="Token contract address or native")
    pay_to: str = Field(default="", alias="payTo", description="Recipient wallet address")
    payer: str = Field(default="", description="Payer wallet address")
    issued_at: int = Field(default=0, alias="issuedAt", description="Unix timestamp (seconds)")
    transaction: str = Field(default="", description="Optional blockchain transaction hash")
    proof_hash: str = Field(
        default="", alias="proofHash", description="Optional payment proof hash"
    )

    model_config = {"extra": "allow", "populate_by_name": True}

    def to_eip712_dict(self) -> dict[str, Any]:
        """Convert to a dict matching the EIP-712 typed data structure.

        Uses aliases (camelCase) for wire format compatibility.
        """
        return self.model_dump(by_alias=True, exclude_none=True)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return self.model_dump_json(by_alias=True)


class SignedReceiptModel(BaseModel):
    """Pydantic model for a signed receipt (format + signature)."""

    format: str = Field(description="Signing format: 'eip712' or 'jws'")
    payload: ReceiptPayloadModel | None = Field(
        default=None, description="EIP-712 payload (omit for JWS)"
    )
    signature: str = Field(description="Hex-encoded ECDSA signature or JWS compact string")

    model_config = {"extra": "allow"}


class AttestationModel(BaseModel):
    """Pydantic model for an attestation wrapping a signed receipt."""

    receipt: SignedReceiptModel = Field(description="The signed receipt")
    attester: str = Field(default="", description="DID or address of the attesting server")
    issued_at: int = Field(
        default=0, alias="issuedAt", description="Attestation timestamp"
    )
    expires_at: int | None = Field(
        default=None, alias="expiresAt", description="Optional expiration timestamp"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata")

    model_config = {"extra": "allow", "populate_by_name": True}


class ReceiptAttestationInfo(BaseModel):
    """Extension info for receipt-attestation in PaymentRequired.extensions."""

    enabled: bool = Field(default=True, description="Whether receipts are generated")
    supported_formats: list[str] = Field(
        default_factory=lambda: ["eip712"],
        alias="supportedFormats",
        description="Supported signing formats",
    )

    model_config = {"extra": "allow", "populate_by_name": True}


class ReceiptAttestationPayload(BaseModel):
    """Full extension payload in settlement response extensions."""

    info: dict[str, Any] = Field(description="Receipt and attestation data")
    schema_: dict[str, Any] = Field(alias="schema", description="JSON Schema")

    model_config = {"extra": "allow", "populate_by_name": True}
