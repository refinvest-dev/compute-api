# Compute Architecture 개선 작업 계획

작성일: 2026-10-28. 검토 기준: Compute `0701c4a`, OpenAPI `0.6.0`.

상태: AR02A까지 구현·검증했다. 후속 단계는 이 브랜치에 포함하지 않는다.

## 1. 목표와 범위

현재 `engine/`, `trading_review/`의 순수 계산 코어를 유지하면서, 바깥 계층을
Hexagonal Architecture로 정리한다. DDD는 도메인 언어·Value Object·정책·불변식을
표현하는 데 적용하고 Aggregate는 실제 일관성 경계가 있을 때 도입한다.

- Application은 concrete PostgreSQL, HTTP, Vendor, 실행 설정을 의존하지 않는다.
- `app/`은 설정·진입점·객체 조립을 담당한다.
- Identity·canonicalization·integrity는 기능과 버전별 계약 모듈에 둔다.
- 공개 결과 projection과 검증된 계산 입력 변환은 application 경계에 둔다.
- DB·Artifact·프로세스 구현은 기능별 port를 구현한다.
- Job 정책과 Snapshot 발행 순서, Core의 소유권을 보존한다.

이번 구조 개선에서 전체 feature 패키지 이동, 범용 Job framework, 공통 Aggregate
기반 클래스, 전역 캐시, 큐 교체, 새 제품 기능은 선제 도입하지 않는다.
Analytics deadline과 Temporal 최적화는 별도 변경으로 검증한다.

## 2. 변경 중 반드시 보존할 기준

| 경계 | 보존할 내용 |
|---|---|
| 계산 | Temporal/Execution 규칙, Decimal·rational 정밀도, expected session 결측 fail-fast |
| 재현성 | Snapshot·VersionSet·FeeModel·기간 pin과 명시적 결과 버전 쌍 |
| 계약 | 요청 검증, status/error/retryable, 멱등성 fingerprint, V1/V2 호환성 |
| 결과 | 누락과 null, Decimal scale, timestamp, 배열 순서, count·section·manifest·result hash |
| Job | 기능별 retry/grant 정책, DB clock, lease/attempt/token/generation 조건부 갱신 |
| 발행 | 소유권 확인과 상태·결과·descriptor의 동일 트랜잭션 저장 |
| 전달 | 바이트 상한, 암호화, exact UTF-8 길이·SHA-256, ACK replay·retention |
| Snapshot | Artifact·manifest 발행 후 metadata 등록, overwrite 금지 |
| 실행 | 부모만 Job 상태 변경, 자식 outcome 반환, spawn·IPC·종료 동작 |
| 운영 | Console script·설정·systemd 진입점과 관측 가능한 오류 분류 |

같은 semantic pin을 유지하는 구조 변경에서는 계산 결과와 semantic hash를 보존한다.
Transport bytes는 runtime ID와 시각까지 고정한 fixture에서 비교한다.
`implementationDigest`는 content-addressed 배포 pin이므로 새 artifact의 digest를 과거 값으로
위장하지 않는다. 배포 digest가 달라질 때의 VersionSet 지원·기존 Job 처리 방안은 정본에
따라 확인하고, pin이 다른 실행 간 hash 동일성을 보존 기준으로 사용하지 않는다.
고정 vector의 기대값을 리팩토링 결과에 맞춰 임의로 바꾸지 않는다.

## 3. 순서와 PR 경계

각 행은 독립적으로 검토·머지할 변경 범위다. 작업 ID는 실제 PR 번호가 아니다.
각 PR에 해당 범위의 테스트와 문서를 함께 포함한다. 표의 완료 조건을 충족한 뒤
체크박스를 갱신하고 후속 경계가 연결되지 않았다면 PR 본문에 명시한다.

| 순서 / ID | 작업·PR 범위 | 선행 조건 | 완료 조건 |
|---|---|---|---|
| 0 / AR-00 | 현재 동작·검증 기준 확보 | 없음 | 계약·회귀·DB·E2E 기준과 미검증 범위 기록 |
| 1 / AR-01 | 계층별 의존성 규칙 강화 | AR-00 | 순수 코어와 새 경계의 금지 의존성을 자동 검출 |
| 2 / AR-02N | Normalization 계약 로직 분리 | AR-01 | 관련 consumer 전환, hash·V1/V2·전달 동작 동일 |
| 2 / AR-02A | Analytics 계약 로직 분리 | AR-01 | 관련 consumer 전환, identity·결과 vector 동일 |
| 3 / AR-03N | Normalization use case·port 분리 | AR-02N | HTTP 경로가 application을 통해 수행됨 |
| 3 / AR-03A | Analytics use case·port 분리 | AR-02A | HTTP 경로가 application을 통해 수행됨 |
| 4 / AR-04 | 공용 payload·ACK 흐름 분리 | AR-03N, AR-03A | route 간 의존 제거, 양쪽 전달·ACK 동작 동일 |
| 5 / AR-05 | Worker application·실행 adapter 정리 | AR-02N/A, AR-03N/A | app 조립과 실행 흐름 분리, 실행 정책 동일 |
| 6 / AR-06 | Persistence 책임 분리 | AR-04, AR-05 | app 역참조 제거, 원자성·동시성 보존 |
| 7 / AR-07 | Snapshot 조회 경계·중복 로딩 개선 | AR-01, 조회 baseline | 요청당 재사용, 결과·무결성·오류 동작 보존 |
| 8 / AR-08 | Analytics 실행 제한 정책 확정 | AR-00부터 검토 가능 | 정본의 deadline·오류·retry·배포 정책 명확화 |
| 9 / AR-09 | 격리 executor와 검증 준비 | AR-05, AR-08 | production 연결 전 프로세스·실패 경계 검증 |
| 10 / AR-10 | Analytics worker 연결·운영 검증 | AR-09 | 부모의 발행·timeout·취소·lease 상실·E2E 검증 |
| 11 / AR-11 | 문서·임시 경로·최종 의존성 정리 | 필수 선행 단계 | 실제 구현과 문서 일치, 미해결 항목 명시 |

기본 착수 순서는 표를 따른다. AR-07은 AR-01 이후 독립적으로 진행할 수 있고,
AR-08의 정책 검토도 AR-00부터 시작할 수 있다. 긴급한 운영 문제가 확인되면
AR-08~10을 앞당기되 선행 검증을 생략하지 않는다.
구조 개선의 완료와 AR-08~10 운영 개선의 완료는 별도로 추적한다.

## 4. 단계별 구현 상태

각 단계는 선행 PR을 base로 하는 draft PR로 제출한다. 검토·머지는 표 순서를 따른다.
PR이 main에 머지되기 전에는 후속 PR을 main 기준으로 독립 머지하지 않는다.

- [x] AR00: architecture-baseline — [ARCHITECTURE_BASELINE_AR00.md](ARCHITECTURE_BASELINE_AR00.md)
- [x] AR01: dependency-guards — [ARCHITECTURE_DEPENDENCIES.md](ARCHITECTURE_DEPENDENCIES.md)
- [x] AR02N: normalization-contracts — [NORMALIZATION_CONTRACTS.md](NORMALIZATION_CONTRACTS.md)
- [x] AR02A: analytics-contracts — [ANALYTICS_CONTRACTS.md](ANALYTICS_CONTRACTS.md)
- [ ] AR03N: normalization-application
- [ ] AR03A: analytics-application
- [ ] AR04: terminal-delivery
- [ ] AR05N: normalization-worker
- [ ] AR05A: analytics-worker
- [ ] AR05B: backtest-process
- [ ] AR06N: normalization-persistence
- [ ] AR06A: analytics-persistence

AR-07 Snapshot 조회, AR-08~10 Analytics 실행 제한·격리·Core E2E, AR-11 최종 정리는 후속 범위다.
