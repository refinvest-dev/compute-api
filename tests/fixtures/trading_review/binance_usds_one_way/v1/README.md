# Legacy test-only fixtures

This directory contains minute-shaped, test-only PoC fixtures that predate the raw-v3 contract.
They are retained only as historical synthetic assets and are not a product contract, runtime
dependency, compatibility oracle, or input to the production engine.

Production tests construct exact-second synthetic bytes and call
`refinvest.trading_review.normalize_and_reconstruct` directly. The authoritative compatibility
oracle is Integration Golden Fixture v3; endpoint-level compatibility remains a follow-up for the
normalization job runtime.
