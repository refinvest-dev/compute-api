# WORKFLOW.md — Compute Agent Workflow

## 1. 작업 시작

새 작업을 받으면 먼저 분류한다.

```text
Engine
Market Data
Ingestion
API
Persistence
Infrastructure
Contract
```

그 다음:

1. 관련 ADR 확인
2. `DOMAIN.md` 의미론 확인
3. `openapi/compute-api.yaml` 계약 확인
4. 영향받는 canonical regression scenario 확인

## 2. 핵심 계산 변경

다음 영역은 임의로 기본값을 만들지 않는다.

- Temporal Rule
- Cross-calendar anchor
- Signal/Lag/Execution semantics
- Missing Session
- Dataset Snapshot
- Fee/Slippage
- Position lifecycle
- Benchmark semantics
- Determinism

스펙 공백이 결과를 바꿀 수 있다면:

1. 가능한 해석을 정리
2. 기존 ADR과 우선순위로 권장안 도출
3. ADR 초안 제안
4. 정책 확정 후 구현

## 3. 일반 구현 결정

정정 비용이 낮은 사항은 기존 코드 convention을 보고 스스로 결정한다.

예:

- 내부 함수 분리
- private helper
- 파일 구성
- 에러 메시지 세부 문구
- 테스트 fixture 구성

매번 확인을 요청하지 않는다.

## 4. 구현 방식

Engine은 계산 단계가 코드에서 드러나게 한다.

정확성을 숨기는 과도한 generic abstraction을 피한다.

Python에서는:

- 타입 힌트를 적극 사용
- dataclass/Pydantic/domain model의 역할을 구분
- mutable global state 금지
- 날짜/시간 기준을 명시
- float precision이 의미 있는 영역은 정책을 확인
- vectorization은 검증 가능한 작은 함수 단위로 적용

FastAPI/Pydantic 모델을 Engine 내부 domain object로 무조건 재사용하지 않는다.

## 5. 테스트 우선순위

핵심 규칙은 example-based + regression test를 둔다.

Temporal 로직에는 boundary/cross-calendar 테스트를 우선한다.

Determinism 변경은 동일 입력 반복 실행 결과를 비교한다.

Missing data test에서는 silent fallback이 없는지 검증한다.

## 6. OpenAPI 변경

Core↔Compute 계약 변경은 compute repo 코드만 수정해서 끝내지 않는다.

먼저 system context의 `openapi/compute-api.yaml`을 갱신하고 Core 영향도를 확인한다.

한쪽 구현만 보고 request/response field를 추측하지 않는다.

## 7. Ingestion 작업

Vendor 특성을 Engine에 노출하지 않는다.

Raw → canonical → snapshot 경계를 유지한다.

Corporate Action/adjustment 정책 변경은 기존 Snapshot 재현성에 미치는 영향을 확인한다.

## 8. 성능 최적화

먼저 profile한다.

금지:

- 미래 데이터를 미리 섞는 최적화
- mutable shared dataframe로 실행 간 상태 공유
- snapshot boundary 무시
- 결과 의미를 바꾸는 approximation

성능 개선 후 동일 regression suite가 통과해야 한다.

## 9. 완료 보고

작업 완료 시 간단히 보고한다.

- 변경 영역
- 관련 ADR/계약
- Determinism 영향
- Temporal/Execution 영향
- 테스트
- 남은 정책 공백

핵심 계산 변경이면 명시적으로:

```text
Determinism impact: none | changed
Point-in-Time impact: none | changed
```

를 포함한다.
