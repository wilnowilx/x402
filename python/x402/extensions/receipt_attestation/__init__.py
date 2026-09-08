"""Receipt Attestation Extension for x402 v2.

Provides cryptographic receipts proving payment was made, enabling:
- Off-chain verification without blockchain lookups
- Portable proof for third-party auditors
- Compliance audit trails

## Usage

### For Resource Servers

```python
from x402.extensions.receipt_attestation import (
    ReceiptGenerator,
    InMemoryReceiptStorage,
    receipt_attestation_server_extension,
)

# Create a receipt generator
storage = InMemoryReceiptStorage()
generator = ReceiptGenerator(
    signing_key=b"your-private-key",
    storage=storage,
    format_="eip712",
    attester="did:web:api.example.com",
)

# Register with server
server = x402ResourceServer(facilitator)
ext = receipt_attestation_server_extension(generator)
server.register_extension(ext)

# Manually generate a receipt (optional)
receipt = await generator.generate(
    network="eip155:8453",
    resource_url="https://api.example.com/premium-data",
    amount="10000",
    asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
    payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
    transaction="0x1234...",
)
```

### For Clients and Verifiers

```python
from x402.extensions.receipt_attestation import ReceiptVerifier

verifier = ReceiptVerifier()
result = await verifier.verify(receipt_data)

if result.valid:
    print(f"Receipt verified: signed by {result.signer}")
else:
    print(f"Verification failed: {result.error}")
```

### For Storage

```python
from x402.extensions.receipt_attestation import InMemoryReceiptStorage, ReceiptStorage

# Use default in-memory storage
storage = InMemoryReceiptStorage()

# Or implement your own (e.g., database-backed)
class PostgresReceiptStorage:
    async def store(self, receipt_id: str, receipt: dict) -> None: ...
    async def retrieve(self, receipt_id: str) -> dict | None: ...
    async def list_by_payer(self, payer: str) -> list[dict]: ...
    async def list_by_resource(self, resource_url: str) -> list[dict]: ...
```
"""

from .client import (
    ReceiptVerifier,
    extract_receipt_from_extensions,
    is_receipt_attestation_extension,
)
from .schema import (
    AttestationModel,
    ReceiptAttestationInfo,
    ReceiptAttestationPayload,
    ReceiptPayloadModel,
    SignedReceiptModel,
    VerificationResult,
    receipt_attestation_schema,
    receipt_payload_schema,
)
from .server import (
    ReceiptAttestationResourceServerExtension,
    ReceiptGenerator,
    receipt_attestation_server_extension,
)
from .storage import InMemoryReceiptStorage, ReceiptStorage
from .types import (
    RECEIPT_ATTESTATION,
    RECEIPT_VERSION,
    AttestationData,
    ReceiptAttestationExtension,
    ReceiptData,
    ReceiptInfo,
)

__all__ = [
    # Constants
    "RECEIPT_ATTESTATION",
    "RECEIPT_VERSION",
    # Data classes
    "ReceiptData",
    "AttestationData",
    # Pydantic models
    "ReceiptInfo",
    "ReceiptAttestationExtension",
    "ReceiptPayloadModel",
    "SignedReceiptModel",
    "AttestationModel",
    "ReceiptAttestationInfo",
    "ReceiptAttestationPayload",
    "VerificationResult",
    # JSON Schemas
    "receipt_attestation_schema",
    "receipt_payload_schema",
    # Storage
    "ReceiptStorage",
    "InMemoryReceiptStorage",
    # Server
    "ReceiptGenerator",
    "ReceiptAttestationResourceServerExtension",
    "receipt_attestation_server_extension",
    # Client
    "ReceiptVerifier",
    "extract_receipt_from_extensions",
    "is_receipt_attestation_extension",
]
