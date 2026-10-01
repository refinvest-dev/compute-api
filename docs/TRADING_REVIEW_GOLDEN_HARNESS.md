# Binance Reconstruction Golden Harness

이 문서는 test-only PoC의 실행과 fixture 관리 방법만 설명한다. 제품 알고리즘의 정본은
`refinvest-system/docs/TRADING_RECONSTRUCTION.md`이며, 이 저장소의 fixture나 harness가 그 규칙을
재정의하지 않는다.

## 실행

```powershell
uv run pytest tests/test_binance_reconstruction_golden.py
```

formatter와 정적 검사는 저장소 표준 명령을 사용한다.

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
```

## Fixture 구조

`tests/fixtures/trading_review/binance_usds_one_way/v1/` 아래에 TR-0A의 25개 시나리오가 각각 한
directory로 존재한다.

```text
scenario-NN-name/
├── trade_history.csv
├── position_history.csv
├── input.json
└── expected.json
```

CSV는 합성 logical fixture다. `input.json`은 source timezone, One-way 선언, pinned instrument terms,
manifest/reconstruction version과 적용한 `TR-I01`~`TR-I13`을 가진다. `expected.json`은 episode,
allocation, reconciliation evidence, derived tolerance, conservation 결과와 canonical manifest hash를
고정한다.

## Expected result 변경

Golden expected는 테스트를 통과시키기 위해 실행 결과로 일괄 덮어쓰지 않는다. 정본 변경 ADR과
scenario별 수작업 검토가 먼저 완료된 경우에만 해당 `expected.json`을 명시적으로 수정한다. 변경 후에는
quantity, fee asset, reported PnL 보존과 이전 manifest hash 변경 이유를 PR에 기록한다.

시나리오 16은 TR-0A 표의 두 same-symbol One-way episode가 시간상 겹치는 모순을
`TR0A_SCENARIO_16_ONE_WAY_OVERLAP`으로 보존한다. 정본이 수정되기 전까지 해당 golden case는 expected
failure이며, harness가 임의 순서나 summary heuristic으로 두 episode를 만들지 않는다.

## 남은 raw 검증

workspace에 실제 익명화 Binance raw fixture가 없으므로 다음 항목은 검증하지 않았고 추측으로 parser를
넓히지 않았다.

- 실제 delimiter, encoding, BOM, line ending, quoting
- 실제 header spelling과 enum 분포
- Binance export에서 `Cross Margin`이 갖는 정확한 raw 의미
- 실제 source scale과 UI 반올림 사례

실제 raw fixture가 제공되면 사용자 식별값과 거래값을 커밋하지 않은 상태에서 별도 검증해야 한다.
