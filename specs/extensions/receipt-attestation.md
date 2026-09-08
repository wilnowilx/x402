# Receipt Attestation Extension

## Summary

The Receipt Attestation Extension provides a concrete implementation layer for the [Offer and Receipt Extension](extension-offer-and-receipt.md). While that specification defines the wire format and cryptographic primitives, this extension adds:

1. **Receipt generation** — A `ReceiptGenerator` that produces signed receipts after successful settlement, integrating with `x402ResourceServer` hooks.
2. **Receipt verification** — A `ReceiptVerifier` that validates receipt signatures and structural integrity off-chain.
3. **Receipt storage** — A pluggable storage interface for persisting receipts (in-memory by default, with a `ReceiptStorage` protocol for custom backends).
4. **Attestation payloads** — Optional extended attestation data (amount, asset, network, payTo, payer, timestamp, transaction hash) that can be included alongside the core receipt.

This extension targets use cases requiring:
- **Off-chain verifiability**: Third parties can confirm payment without blockchain lookups.
- **Portable proof**: Clients can present receipts to auditors, dispute systems, or reputation platforms.
- **Compliance audit trails**: Servers and clients retain signed evidence of commercial interactions.

## Use Cases

### Agent-to-Agent Commerce

An autonomous agent purchases data from a service. The agent's operator later audits the transaction using the signed receipt, confirming the agent received the promised service without needing on-chain access.

### Dispute Resolution

A customer disputes a payment. The server produces a signed receipt with the transaction hash, proving payment was received and service was delivered. The receipt is independently verifiable by the dispute mediator.

### Reputation and Discovery

A third-party platform aggregates signed receipts from multiple services to build reputation scores. Receipts prove actual payment and service delivery, preventing fake reviews.

### Compliance and Audit

A regulated entity needs to demonstrate payment receipts for tax or regulatory purposes. Signed receipts provide tamper-evident evidence of all commercial transactions.

## Specification

### 1. Extension Key

```
RECEIPT_ATTESTATION = "receipt-attestation"
```

### 2. Receipt Data Model

A receipt contains the following fields:

| Field           | Type     | Required | Description                                              |
| --------------- | -------- | -------- | -------------------------------------------------------- |
| `version`       | number   | Yes      | Receipt schema version (currently `1`)                   |
| `network`       | string   | Yes      | Blockchain network in CAIP-2 format (e.g., `eip155:8453`) |
| `resourceUrl`   | string   | Yes      | The paid resource URL                                    |
| `amount`        | string   | Yes      | Payment amount (smallest unit)                           |
| `asset`         | string   | Yes      | Token contract address or `native`                       |
| `payTo`         | string   | Yes      | Recipient wallet address                                 |
| `payer`         | string   | Yes      | Payer wallet address                                     |
| `issuedAt`      | number   | Yes      | Unix timestamp (seconds) when receipt was issued         |
| `transaction`   | string   | Optional | Blockchain transaction hash                              |
| `proofHash`     | string   | Optional | Hash of the payment proof for additional integrity        |

### 3. Attestation Data Model

An attestation wraps a receipt with optional server metadata:

| Field        | Type   | Required | Description                                  |
| ------------ | ------ | -------- | -------------------------------------------- |
| `receipt`    | object | Yes      | The signed receipt (EIP-712 or JWS format)   |
| `attester`   | string | No       | DID or address of the attesting server       |
| `issuedAt`   | number | Yes      | Timestamp of the attestation                 |
| `expiresAt`  | number | No       | Optional expiration timestamp                |
| `metadata`   | object | No       | Arbitrary key-value metadata                 |

### 4. Signing Format

Receipts MUST be signed using either:

- **EIP-712**: Using the domain `name: "x402 receipt"`, `version: "1"`, `chainId: 1` as defined in [extension-offer-and-receipt.md §3.2](extension-offer-and-receipt.md).
- **JWS**: Using ES256K or EdDSA with a `kid` header pointing to a DID or public key.

### 5. Storage Protocol

Implementations MUST provide a `ReceiptStorage` protocol:

```python
class ReceiptStorage(Protocol):
    async def store(self, receipt_id: str, receipt: dict[str, Any]) -> None: ...
    async def retrieve(self, receipt_id: str) -> dict[str, Any] | None: ...
    async def list_by_payer(self, payer: str) -> list[dict[str, Any]]: ...
    async def list_by_resource(self, resource_url: str) -> list[dict[str, Any]]: ...
```

The default implementation uses an in-memory dictionary suitable for development and testing. Production deployments SHOULD implement persistent storage (database, filesystem, etc.).

### 6. Server Integration

The `ReceiptGenerator` hooks into `x402ResourceServer.on_after_settle()` to automatically generate receipts after successful settlement:

```python
from x402.extensions.receipt_attestation import ReceiptGenerator, receipt_attestation_extension

generator = ReceiptGenerator(
    signing_key=private_key,
    storage=InMemoryReceiptStorage(),
)

server = x402ResourceServer(facilitator)
server.register_extension(receipt_attestation_extension(generator))
```

### 7. Client Verification

Clients and third parties use `ReceiptVerifier` to validate receipts:

```python
from x402.extensions.receipt_attestation import ReceiptVerifier

verifier = ReceiptVerifier()
result = await verifier.verify(receipt_data)

if result.valid:
    print(f"Receipt verified: signed by {result.signer}")
```

### 8. JSON Wire Format

Receipt attestation data is placed in the `extensions` field of the settlement response:

```json
{
  "extensions": {
    "receipt-attestation": {
      "info": {
        "receipt": {
          "format": "eip712",
          "payload": {
            "version": 1,
            "network": "eip155:8453",
            "resourceUrl": "https://api.example.com/premium-data",
            "amount": "10000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            "payer": "0x857b06519E91e3A54538791bDbb0E22373e36b66",
            "issuedAt": 1703123456,
            "transaction": "0x1234...",
            "proofHash": "0xabcd..."
          },
          "signature": "0x..."
        },
        "attester": "did:web:api.example.com",
        "issuedAt": 1703123456
      }
    }
  }
}
```

## Security Considerations

- Receipts are signed artifacts. Possession of a valid signature is sufficient for verification. Transport-layer security (HTTPS) is essential.
- Servers SHOULD use a dedicated signing key separate from the `payTo` address to limit key compromise risk.
- Receipts without the `transaction` field are privacy-minimal; verifiers SHOULD be aware that the absence of a transaction hash limits on-chain verification.
- Receipts are transferable; implementations SHOULD consider replay and revocation implications.

## Privacy Considerations

- Receipts are minimal by default — they include only the fields necessary for verification.
- The `payer` field is included for audit trails but may be omitted in privacy-sensitive deployments.
- Servers MAY omit the `transaction` field when verifiability is less important than privacy.

## Version History

| Version | Date       | Changes                            | Author          |
| ------- | ---------- | ---------------------------------- | --------------- |
| 0.1     | 2026-09-08 | Initial extension draft.           | wilnowilx       |
