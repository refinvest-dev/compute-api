# Trading Review normalization/reconstruction engine

The production pure-computation entrypoint is:

```python
from refinvest.trading_review import normalize_and_reconstruct

result = normalize_and_reconstruct(request)
```

`NormalizationRequest` pins exactly one Trade History artifact, one Position History artifact,
source timezone and timezone-database version, One-way mode, immutable instrument terms, identity
key version, optional fee valuations, and the exact registered `NormalizationVersionSet`. The
engine performs no network, database, filesystem, clock, UUID, FastAPI, worker, or sibling-repository
operation.

The pipeline is explicit:

```text
bytes/parser validation
→ canonical record construction and identity conflict detection
→ independent artifact coverage
→ one-way signed-position reconstruction
→ position-summary candidate graph reconciliation
→ conservation verification
→ capability and quality reports
→ manifest and result hashes
```

All financial values use `Decimal` through `ScaledDecimal`; source lexeme and scale are retained.
Average prices are reduced exact rationals. UTC timestamps are timezone-aware and serialized with
six fractional digits. Canonical JSON uses UTF-8, Unicode-code-point object key order, semantic tuple
order, and no insignificant whitespace.

Hash inputs include source artifact role/dialect/content hash, source timezone and pinned timezone
database version, identity key version, instrument terms, fee valuations, canonical records,
episodes/allocations, tolerance evidence, reconciliation issues, capability, quality, and every
semantic VersionSet component. Runtime artifact IDs, raw bytes, generated/created/terminal times,
worker/storage metadata, self hashes, and private source Trade-ID ordering fields are excluded.

The package never imports `tests`, the Integration sibling checkout, FastAPI, persistence, vendor
SDKs, or backtest execution models. The historical `tests/trading_review_harness` PoC is retired and
its suite is skipped; production regression tests call this entrypoint directly. Integration Golden
Fixture v3 remains the compatibility oracle. Endpoint-level fixture E2E belongs to the future
normalization job runtime, not this pure engine.
