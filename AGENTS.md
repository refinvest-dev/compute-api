# AGENTS.md — refinvest-compute

> Codex 및 기타 코딩 에이전트가 `refinvest-compute`에서 작업하기 전에 반드시 읽는 진입 문서다.

## 1. 역할

`refinvest-compute`는 **Backtest Engine + Market Data Ingestion**을 담당하는 내부 계산 서비스다.

- Stack: Python + FastAPI
- 호출자: `refinvest-core`만
- Web은 Compute를 직접 호출하지 않는다.
- User / Member / Subscription / Strategy 저장 상태를 소유하지 않는다.
- Backtest 결과를 장기 영속화하지 않는다. 계산 결과를 Core에 반환한다.
- Asset / Calendar / DatasetSnapshot 같은 시장 참조 데이터는 Compute가 소유한다.

Compute의 최우선 목표는 기능 수가 아니라 **정확하고 재현 가능한 계산**이다.

## 2. 읽기 순서

작업 전 다음을 확인한다.

1. `AGENTS.md`
2. `docs/WORKFLOW.md`
3. `docs/DOMAIN.md`
4. `docs/ARCHITECTURE.md`
5. `openapi/compute-api.yaml`
6. system context의 관련 ADR/정본 문서

계산 의미론을 바꾸는 작업에서는 관련 ADR을 반드시 확인한다.

## 3. 절대 원칙

### Determinism

동일한:

```text
StrategyVersion Payload
+ Dataset Snapshot
+ Engine Version
+ Fee/Slippage Model
```

은 항상 동일한 Backtest Result를 만들어야 한다.

실행 시점의 실시간 데이터나 mutable latest state를 계산 중간에 섞지 않는다.

### Point-in-Time Correctness

Signal Timestamp 이후의 데이터를 계산에 사용하지 않는다.

Cross-calendar metric은 해당 시점 이전에 **이미 완료된 가장 최근 세션**만 참조한다.

### No Silent Correction

Expected Session 데이터가 없으면:

- 이전 값 복제
- interpolation
- 임의 가격 생성
- 자동 fallback

을 하지 않고 fail-fast한다.

### Explicit Primary Signal Asset

Primary Signal Asset은 입력에 명시되어야 한다. 조건 순서나 Execution Asset으로 추론하지 않는다.

## 4. Temporal Contract

모든 시간 계산은 다음 규칙을 따른다.

| 요소 | 기준 |
|---|---|
| Metric Window | Referenced Asset 자신의 Trading Session |
| Signal Timestamp | Primary Signal Asset Calendar |
| Lag | Primary Signal Asset Signal Session |
| Holding Period | Primary Signal Asset Signal Session |
| Entry / Exit Execution | Execution Asset의 Next Available Session |

이 규칙을 최적화나 편의를 위해 변형하지 않는다.

## 5. MVP 범위

지원 Asset:

```text
QQQ
SPY
TQQQ
SOXL
BTCUSDT
```

MVP는 Daily Bar만 지원한다.

지원:

- SIMPLE / RETURN / CHANGE
- Relative 조건
- AND / OR 단일 Condition Group
- Lag
- Time-based Exit
- Long Only
- Single Active Position
- Duplicate Entry Ignore
- 고정 Fee / Slippage
- Next Available Session execution
- Open reference price

지원하지 않음:

- Intraday
- Short
- Multi-position
- Condition-based Exit
- Portfolio Allocation
- AI 계산
- 미지원 indicator의 자동 대체

## 6. Engine / Ingestion 경계

Backtest Engine과 Data Ingestion을 강하게 분리한다.

```text
engine/
ingestion/
```

Ingestion 실패나 vendor SDK가 Engine 계산 로직에 침투하지 않게 한다.

Engine은 vendor API를 직접 호출하지 않는다. 반드시 immutable DatasetSnapshot을 입력으로 사용한다.

## 7. Dataset Snapshot

DatasetSnapshot은 immutable이다.

새 데이터가 들어오면 기존 Snapshot을 수정하지 않고 새 Snapshot을 생성한다.

과거 재현 요청에서 `datasetSnapshotId`가 명시되면 반드시 해당 Snapshot을 사용한다.

명시되지 않으면 실행 시작 시점에 하나의 Snapshot을 선택하고, 그 이후 해당 실행에서는 변경하지 않는다.

## 8. Execution

Reference Price:

```text
Execution Session Open
```

Slippage:

```text
Buy  = Open × (1 + slippage)
Sell = Open × (1 - slippage)
```

FeeModel과 execution rule은 결과 재현성의 일부다.

이미 포지션을 보유한 상태의 중복 Entry Signal은 무시한다.

## 9. Error / Result

Expected Session 결측 등 실행 중 데이터 오류는 fail-fast한다.

Fatal error 범주는 OpenAPI 계약을 따른다.

Zero Trades는 오류가 아니다.

```text
tradeCount == 0
→ COMPLETED
→ sampleSizeWarning = ZERO
```

Trade Count < 10은 LOW sample warning으로 처리한다.

Benchmark:

- Primary = Execution Asset Buy & Hold
- Secondary = Signal Asset Buy & Hold (Primary Signal Asset이 Execution Asset과 다를 때 참고용)

## 10. Internal API Security

Compute API는 외부 공개 API가 아니다.

Core→Compute 요청은 `X-Internal-Api-Key` 기반 내부 인증을 사용한다.

- Key 하드코딩 금지
- TLS와 네트워크 격리는 별도 방어선
- mTLS는 현재 범위 밖

## 11. 작업 원칙

- OpenAPI 계약을 임의로 변경하지 않는다.
- 스펙에 없는 핵심 계산 의미론을 임의로 선택하지 않는다.
- 계산 의미론이 모호하면 코드를 먼저 작성하지 말고 관련 ADR 초안을 제안한다.
- 미완성 미래 Phase 기능을 선제 구현하지 않는다.
- 벡터화는 성능 수단일 뿐 정확성보다 우선하지 않는다.
- 과도한 추상화보다 명확한 계산 흐름을 우선한다.
- `PR_BODY.md`는 로컬 PR 작성 보조 파일이다. 어떤 경우에도 Git에 add, commit, push하지 않는다.
- 커밋 메시지의 괄호 안 scope는 저장소 이름이 아니라 이 저장소 안에서 변경한 도메인이나 작업 단위를 나타낸다. 예: `feat(trading-review): ...`, `fix(ingestion): ...`. `feat(compute): ...`처럼 저장소 이름을 scope로 쓰지 않는다.
- 큰 작업은 코드를 쓰기 전에 **커밋과 PR의 분할안**을 세운다. API 계약, 입력 검증, 계산 연결, persistence, worker, 결과 전달·운영처럼 독립적으로 검증할 수 있는 경계를 찾고, 각 단계의 테스트와 후속 연결 지점을 명시한다.
- 커밋 하나에는 설명 가능한 변경 한 가지와 그 검증을 담는다. 구현 전체를 마지막 단일 커밋에 몰지 않는다. 선행 migration, 순수 adapter와 fixed vector 검증, durable job 상태, worker·결과 전달처럼 단계별로 커밋하되 각 커밋에서 관련 테스트가 통과하고 기존 동작이 유지돼야 한다.
- PR은 기존 동작을 깨지 않고 독립적으로 검토·머지할 수 있는 범위로 만든다. 큰 runtime은 준비 단계가 당장 사용자에게 노출되지 않아도 안전한 계약 검증·additive migration·순수 구성 요소를 먼저 머지할 수 있다. 이때 후속 PR과 아직 연결되지 않은 경계를 본문에 명시한다.
- API·persistence·worker·delivery 등 여러 경계에 걸쳐 diff가 커질 조짐이 보이면 먼저 여러 커밋과 PR로 나눌 수 있는지 검토한다. 한 PR로 유지해야 하는 원자적 배포·호환성 이유가 있다면 PR 본문에 기록한다.
- 예를 들어 Analytics Job Runtime이라면 입력 identity·result projection과 fixed vector 검증, durable job API·migration과 DB 테스트, worker·terminal delivery·ACK와 운영 검증을 각각 검토 가능한 단계로 나눌 수 있다. 이 예시를 고정 템플릿으로 적용하지 않고 실제 의존성과 안전한 머지 순서를 확인한다.
- 파일 종류만을 기준으로 구현과 필수 테스트·문서를 기계적으로 분리하지 않는다. 각 단계의 의미와 검증이 함께 이해되도록 묶고, 불완전한 production 경로를 활성화한 채 머지하지 않는다.

우선순위:

```text
Data Correctness
> Temporal Correctness
> Execution Correctness
> Statistical Interpretation
> Performance
> Feature Count
```

## 12. 완료 전 검증

관련 작업에 맞게 실행한다.

- formatter / linter
- type checker
- unit tests
- integration tests
- full test suite

Temporal/Execution 변경은 반드시 결정론 및 cross-calendar regression test로 검증한다.

## 13. Cross-repository integration

- `../../integration`은 Compute의 runtime dependency나 Git submodule이 아닌, sibling checkout을
  조립해 검증하는 local E2E harness다. Compute source tree나 Python import path에 포함하지 않는다.
- Core↔Compute 호출 또는 OpenAPI snapshot에 영향을 주는 변경은 context 정본을 먼저
  확인하고, 구현 전후 `../../integration/scripts/verify-contract-sync.ps1`을 실행한다.
- Compute 동작 변경은 integration의 분리된 DB와 non-production fixture로 Core dispatch부터
  terminal result까지 smoke를 검증한다. 로컬 credential, fixture 산출물, compatibility evidence는
  Compute 저장소에 커밋하지 않는다.
- integration harness의 기동 절차·환경 변수·실행 증적은 `../../integration/README.md`와
  `../../integration/AGENTS.md`를 정본으로 하며, 이 파일에는 중복해서 관리하지 않는다.
