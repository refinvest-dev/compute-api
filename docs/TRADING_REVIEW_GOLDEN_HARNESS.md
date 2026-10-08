# Binance Reconstruction Golden Harness

이 문서가 설명하던 `tests/trading_review_harness` 독립 계산 PoC는 운영 엔진 도입으로 retired 상태다.
기존 minute-shaped 합성 fixture는 역사적 test-only 자산으로만 남기며 제품 registry, runtime dependency,
compatibility oracle 또는 운영 결과 생성기로 사용하지 않는다. 기존 PoC suite는 명시적으로 skip된다.

운영 계산의 정본은 Context의 `TRADING_REVIEW.md`, `TRADING_RECONSTRUCTION.md`, ADR-058/059와
OpenAPI 0.3.0이다. 구현 경계와 hash 입력은 `docs/TRADING_REVIEW_ENGINE.md`를 따른다.

## Production regression 실행

```powershell
uv run pytest tests/test_trading_review_parser.py `
  tests/test_trading_review_engine.py `
  tests/test_trading_review_determinism.py `
  tests/test_trading_review_invariants.py
```

이 테스트는 `src/refinvest/trading_review`의 공개 entrypoint
`normalize_and_reconstruct(NormalizationRequest)`를 직접 호출한다. PoC harness 결과를 운영 결과로
반환하거나 비교하지 않는다.

## Compatibility oracle

Integration Golden Fixture v3가 raw byte contract와 expected compatibility evidence의 정본이다.

```powershell
cd ..\..\integration
.\scripts\verify-contract-sync.ps1 -IncludeWeb
.\scripts\verify-trading-review-fixtures.ps1 -Dataset raw-v3
```

fixture validator는 authored fixture 자체의 schema/checksum/provenance/보존식과 corruption rejection을
검증하며 production engine을 실행하지 않는다. normalization job API가 추가된 뒤 별도 integration E2E로
실제 engine compatibility를 검증한다.
