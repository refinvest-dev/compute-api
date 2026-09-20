# Compute Backtest benchmarking

`refinvest-benchmark`는 고정된 integration fixture와 scenario로 Compute의 실제 Daily Bar
application/engine pipeline을 반복 실행한다. 목적은 결과 결정성을 correctness gate로 확인하고 단계별
시간, raw sample, 요약 통계를 남기는 것이다. 성능 최적화, production latency SLO, 공식 baseline을
정의하는 도구가 아니다. 실제 요청의 처리량과 상태를 보는 runtime observability와도 별개다.

## 입력 소유권과 준비

fixture definition, expected manifest, 여섯 scenario는 sibling `integration/performance/`가 소유한다.
Compute는 그 schema를 재정의하거나 artifact를 복제하지 않고 input adapter로 읽는다. 먼저 integration
정본 절차로 fixture를 publish/verify한다.

```powershell
cd ..\..\integration
.\scripts\publish-performance-fixture.ps1 `
  -EnvironmentFile .\.env.local `
  -ComputeApiPath ..\compute-api\refinvest
```

기본 artifact 위치는 integration의
`.integration-data/performance-snapshots/performance-mvp-daily-v1`이다. 이 디렉터리, 환경 파일,
credential, benchmark report는 Git에 추가하지 않는다.

## 실행

```powershell
uv run refinvest-benchmark `
  --fixture-manifest ..\..\integration\performance\fixture-manifest.json `
  --snapshot-path ..\..\integration\.integration-data\performance-snapshots\performance-mvp-daily-v1 `
  --scenario ..\..\integration\performance\scenarios\simple-1y.json `
  --warmups 3 `
  --iterations 10 `
  --cache-mode warm `
  --output ..\..\integration\.integration-data\reports\simple-1y.json
```

`--expected-results <path>`를 추가하면 fixture ID, scenario ID, canonicalization version, terminal
status, trade count, sample warning, result hash를 모두 strict 비교한다. 하나라도 다르면 report를 쓴 뒤
종료 코드가 0이 아니다. expected 파일을 자동 생성하거나 갱신하는 `accept`/`update-golden` 경로는
제공하지 않는다.

fixture manifest는 version, 고정 snapshot metadata, 기간/asset, adjustment policy, calendar
provenance, 선언된 artifact와 SHA-256/size, storage-independent Compute manifest hash를 제공한다.
`--snapshot-path`는 integration 절대 경로를 manifest에 넣지 않기 위해 별도로 전달한다. runner는
정규화된 단일 파일명 `calendar.json`, `daily-bars.ndjson`, `manifest.json`만 허용하고 path traversal을
거부한다. 각 실행에서 외부 manifest hash/size를 검증한 후 기존 Compute filesystem snapshot loader가
내부 manifest와 provenance를 다시 검증한다. latest snapshot, PostgreSQL, vendor API, credential은
사용하지 않는다.

scenario의 `request`는 별도 benchmark DSL이 아니다. 기존 Pydantic
`CreateBacktestRequestTransport`, domain conversion과 strategy validation을 그대로 통과한다.
run ID는 `benchmark:<scenarioId>:<warmup|iteration>:<one-based-index>`로 결정적으로 만든다. 이는
operational identity이며 canonical result에는 포함되지 않는다.

## 측정 경계

모든 시간은 `time.perf_counter_ns()`로 측정하고 report에는 음수가 아닌 integer nanoseconds로
저장한다. observer가 없을 때 production executor의 결과와 예외 의미는 기존과 같다. stage observer의
오류는 원 계산 오류를 가리지 않는다.

- `snapshot_load`: integration manifest 재검증 시작부터 선언 artifact hash/size 검증, 기존 Compute
  loader의 내부 manifest/provenance 검증 및 immutable execution data 구성 완료까지.
- `request_strategy_validation`: scenario `request`의 Pydantic parsing, domain conversion, Strategy
  validation 완료까지. fixture/scenario 파일 생성시간은 제외한다.
- `calendar_resolution`: actual period 교집합과 Primary Signal Asset 평가 session 범위 확정까지.
- `signal_evaluation`: condition 평가 시작부터 ordered signal tuple 확정까지.
- `entry_exit_resolution`: lag, holding, next-available session, single-position admission과 entry/exit open
  price/slippage resolution 완료까지. 현재 pipeline에서는 position admission과 execution pricing이 이
  단계에 포함된다.
- `trade_realization`: commission을 적용해 ordered trades를 확정할 때까지.
- `performance_benchmark_assembly`: strategy equity/metrics, Execution Asset B&H 및 선택적 Signal Asset
  reference, data integrity와 result domain assembly 완료까지. 여기서 benchmark는 측정 harness가 아닌
  BacktestResult의 시장 reference다.
- `result_serialization`: production result payload assembly와 canonical UTF-8 JSON byte encoding의 합.
  두 구간을 같은 stage에 누적하며 계산을 다시 실행하지 않는다.
- `total_execution`: transport validation 시작부터 canonical bytes 완료까지. 위 stage 사이 orchestration을
  포함하고 CLI parsing, fixture publication, queue wait, report 쓰기는 제외한다.

stage 합과 total은 orchestration 및 timer/allocation 측정 overhead 때문에 같지 않을 수 있다.

## Warm-up, cache와 memory

warm-up은 0 이상이고 측정 sample과 percentile에서 제외한다. 다만 functional failure 또는 warm-up과
측정 iteration 사이 result hash 불일치는 전체 correctness 실패다. iteration은 양수이며 실패 sample도
report에 남고 성공 표본처럼 조용히 제거되지 않는다.

`warm`은 하나의 runner process에서 지정 warm-up 후 반복한다. Python/OS filesystem cache가 따뜻해질
수 있다는 의미일 뿐 특정 cache 상태를 보장하지 않는다. `process-cold`는 현재 미지원이며 명시적으로
오류를 낸다. 이를 warm으로 대체하지 않으며 OS page cache가 비워진다는 주장도 하지 않는다.

측정 iteration마다 `tracemalloc.start()`, `reset_peak()`, `get_traced_memory()`, `stop()` 경계를 새로
만든다. `pythonPeakAllocatedBytes`는 이 경계의 Python-tracked peak allocation이며 process RSS나 시스템
peak memory가 아니다. warm-up memory는 포함하지 않는다.

## Canonicalization version 1

version 1은 calculation-bearing top-level field를 명시적으로 선택한다: `metrics`, `equityCurve`,
`trades`, `benchmark`, `signalExecutionMarketRelation`, `signalExecutionDelay`, `sampleSizeWarning`,
`dataIntegrityStatus`. `dataIntegrityStatus.datasetSnapshotId`도 포함된다.

UTF-8 JSON, lexicographical object key order, compact separators, preserved array order와 null을 사용한다.
기존 transport payload의 Decimal string, ISO date, UTC ISO timestamp 표현을 보존한다. top-level
`backtestRunId`만 제외하며 nested `runId`를 재귀 삭제하지 않는다. iteration, duration, memory,
environment와 실행 시각은 포함하지 않는다. SHA-256 lowercase hex를 `sha256:<hex>`로 표시한다.
version이 다르면 비교하지 않고 실패한다.

## 통계와 report

report version은 1이다. fixture/scenario identity, cache mode, warm-up/iteration, 실행 환경과 Compute
revision, raw sample, 성공/실패 수, total/stage summary, Python allocation summary, correctness/mismatch를
기록한다. request와 result 원문, DB URL, API key, token은 기록하지 않는다. Git revision을 읽지 못하면
`null`이며 benchmark를 실패시키지 않는다. report는 같은 디렉터리의 임시 파일을 flush한 뒤 atomic
replace하고 parent가 없으면 생성한다.

각 duration summary는 min/max/median/p95를 보존한다. p95는 nearest-rank,
`ceil(0.95 * N)`번째 정렬 표본이다. 짝수 표본 median의 두 integer nanosecond 중간값이 0.5ns이면
아래 integer로 내린다. 표시용 ms 변환이 필요하면 원본 ns를 바꾸지 않고 소수점 셋째 자리에서
반올림한다. Python allocation은 median/max를 기록한다. 작은 N도 report에 그대로 남긴다.

공유 CI runner의 결과는 환경 noise 때문에 참고값일 뿐 production SLO나 자동 performance budget이
아니다. 공식 golden과 baseline은 후속 단계에서 사람이 candidate terminal status/trade count/hash와
계산 의미론을 검토한 뒤 integration 소유 expected-results 및 통제된 baseline으로 확정한다.
