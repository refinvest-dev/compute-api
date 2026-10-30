# Compute Architecture 개선 작업 계획

작성일: 2026-10-28. 검토 기준: Compute `0701c4a`, OpenAPI `0.6.0`.

상태: AR-00 완료. 기존 formatter 차이 24개는 포맷 전용 보완과 AST·주석 동일성 검사,
formatter·lint·Pyright 재검증으로 해소했다. AR-01 의존성 규칙 강화도 완료했으며
AR-02N Normalization·AR-02A Analytics 계약 분리와 AR-03N/03A application·HTTP 연결도 완료했다.
AR-04 공용 payload·ACK 조율과 AR-05N Normalization worker·실행 adapter 분리도 완료했다.
AR-05A Analytics worker·Artifact adapter와 AR-05B Backtest 실행 adapter 분리도 완료했다.
AR-06N Normalization persistence 책임 분리와 실 DB·단일 전체 회귀·고정 Core E2E 검증도 완료했다.
AR-06A Analytics persistence 분리와 전후 비교·실 DB 검증도 완료했다.
AR-07 Snapshot 조회 port·provider 연결과 요청 내 단일 로딩, 마지막 legacy 예외 제거도 완료했다.
다음은 AR-08 Analytics 실행 제한 정책이며 구현 전에 Context ADR 초안을 검토한다.
실행 결과와 남은 gate는 [AR-00 baseline 보고서](ARCHITECTURE_BASELINE_AR00.md)에 기록한다.
이 문서는 Compute 로컬 실행 계획이다. 제품 정책·ADR·API·hash fixture의 정본은
`../../context`이며 이 문서로 변경하지 않는다.

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

## 4. 단계별 작업 목록

### AR-00 — 현재 동작과 검증 기준 확보

목적은 후속 변경 전후를 비교할 수 있는 실행 기준을 확보하는 것이다.
기존 동작을 확인하고 검증 공백을 식별하며, 구조 변경·정책 변경·성능 최적화는 수행하지 않는다.
아래 A → B → C → D → E 순서로 진행한다. 완료 표시는 실행 결과를 기준으로 갱신한다.

#### A. 비교 대상과 실행 환경 고정

- [x] Compute·Context·Core·Integration의 commit과 작업 트리 변경 여부를 기록한다.
- [x] Compute의 `openapi/CONTEXT_REVISION`과 실제 Context revision을 구분해 기록한다.
- [x] OS·Python·의존성 lockfile identity와 테스트 도구 버전을 기록한다.
- [x] 지원하는 Normalization V1/V1·V2/V2, Analytics VersionSet와 배포 pin을 목록화한다.
- [x] 사용할 합성 fixture와 immutable Snapshot의 ID·버전·hash를 기록한다.
- [x] 격리 DB 대상·schema·migration 적용 범위와 재현 명령을 확인한다.

산출물은 실행 manifest다. Credential·환경 파일 본문·signed locator·원본 거래값은 포함하지 않는다.
Commit만으로 재현되지 않는 tracked runtime 변경이 있다면 비교 대상에 포함되는지 명시한다.
문서나 PR 보조 파일의 변경을 runtime 코드 변경과 구분하며 사용자 변경을 stash/reset하지 않는다.

#### B. 계약과 로컬 계산 기준 확인

- [x] Integration의 contract-sync를 실행해 정본과 실제 테스트 대상의 일치를 확인한다.
- [x] Ruff, 변경 대상의 formatter 기준, Pyright, 전체 pytest 결과를 수집한다.
- [x] 기존 테스트 중 다음 보존 항목을 어느 테스트가 확인하는지 coverage 표를 만든다.

| 영역 | 확인할 기준 |
|---|---|
| Backtest | 결정론, cross-calendar, lag/holding, next session execution, 결측 fail-fast, ZERO/LOW |
| Normalization | input identity, V1/V2 결과 쌍, count·section·manifest·result hash, 입력 거절 |
| Analytics | 세 Metric의 fixed vector, 입력 hash, 결과 projection·digest, capability/sample 상태 |
| 전달 | exact bytes·상한·SHA-256, descriptor, ACK replay, 오류 분류 |

고정 vector는 정본 기대값을 사용한다. 런타임 ID·시각이 다른 실행의 전체 bytes를 서로
같다고 요구하지 않는다. 기존 테스트가 이 기준을 검증하면 재사용하고, 후속 리팩토링에
직접 영향을 주는 고위험 공백만 작은 characterization test로 보완한다.
추가 테스트와 문서는 같은 검증 범위의 커밋으로 묶는다.

#### C. 세 Job의 PostgreSQL 통합 기준 확인

- [x] 격리 PostgreSQL에 현재 테스트가 요구하는 migration을 준비한다.
- [x] Backtest·Normalization·Analytics 통합 테스트를 실제 실행한다.
- [x] 중복 접수·claim, 기능별 retry/grant, lease 만료·교체, stale 발행 거절을 확인한다.
- [x] 취소와 완료의 경쟁, 결과·descriptor·상태의 원자성, ACK·cleanup을 확인한다.
- [x] Normalization·Analytics 공유 admission capacity와 기존 integrity trigger를 확인한다.

테스트에 이미 포함된 시나리오와 별도 보완이 필요한 시나리오를 구분한다. DB 연결 설정만
있거나 테스트가 수집되었다는 사실로 완료 처리하지 않는다. Test docstring의 과거 migration
번호를 그대로 적용하지 않고 현재 schema 요구사항을 확인한다.

#### D. 현재 지원되는 Core 연동 기준 확인

- [x] Backtest는 Core dispatch → Compute terminal → Core 결과 저장까지 기존 smoke로 확인한다.
- [x] Normalization은 기존 runtime/publication/version-pin harness에서 현재 지원되는 조합을 선택한다.
- [x] API·worker 시작, 결과 수신·발행·ACK까지 각 harness가 실제 검증한 경로를 기록한다.
- [x] Analytics는 fixed vector·worker/API 모의 테스트·PostgreSQL 통합 결과를 구분한다.
- [x] Analytics Core E2E 등 없는 검증은 후속 담당 단계와 연결하고, 실행했다고 기록하지 않는다.

Core publication이 구현되지 않은 버전 조합을 AR-00에서 새로 구현하지 않는다.
예를 들어 Compute의 V2 생산 지원과 Core의 V2 publication 완료는 별도 항목이다.
지원 여부가 불명확하면 Context·Core 구현·현재 harness를 대조해 검증 가능 범위를 먼저 정한다.
새 Analytics Core harness가 필요하면 Integration 소유 작업으로 두고 AR-10의 운영 검증에 연결한다.

#### E. 기준선과 검증 공백 기록

- [x] 검사별 명령·실행 시각·대상 revision·결과·증적 경로를 기록한다.
- [x] 결과 상태를 PASS / FAIL / SKIPPED / NOT_RUN / NOT_SUPPORTED로 구분한다.
- [x] 공백마다 영향받는 변경, 담당 단계, 해당 검증이 필요한 시점을 기록한다.
- [x] 기존 실패는 원인과 후속 처리 범위를 적고 리팩토링 변경과 분리한다.
- [x] 기존 formatter 차이를 별도 포맷 보완으로 정리하고 AST·주석 동일성 및 정적 검사를 확인한다.

보고서는 버전 조합·검증 coverage·요약 결과·공백과 후속 단계로 구성한다.
Compute가 소유하는 비민감 재현 절차·coverage·요약은 저장소 문서로 관리할 수 있다.
실행 로그·로컬 fixture·DB 산출물·compatibility evidence는 Integration 규칙의 로컬 위치에
보관하고 Compute에 커밋하지 않는다. 기동·환경·DB 절차는 Integration 문서를 참조한다.

**완료 조건:** A~E를 확인하고, 현재 지원되는 필수 계약·계산·DB·Core smoke에서
미해결 FAIL·검증 누락이 없어야 한다. 실제 미지원 기능과 아직 없는 Analytics Core E2E는
담당 단계·완료 gate를 명시해 별도 추적할 수 있지만 PASS로 계산하지 않는다.
지원 경로의 필수 검사가 환경 문제로 실행되지 않으면 AR-00은 부분 완료로 남긴다.
그동안 문서·의존성 검사·순수 계약 준비는 진행할 수 있으며, DB/worker 변경을 검증 완료로
판단하거나 동작 보존을 확인했다고 보고하지 않는다.

앞선 검토의 `463 passed / 117 skipped`는 과거 기록이다. 새 baseline으로 재사용하지 않는다.
`/assets` 로딩 횟수와 Snapshot 성능 측정은 AR-07 착수 직전에, Temporal profile은 해당
선택적 최적화 착수 직전에 확보한다. Load/stress·전체 crash recovery 조합·새 harness 구축을
AR-00의 일괄 선행 작업으로 확대하지 않으며, 변경 위험에 따라 해당 단계에 배정한다.

### AR-01 — 의존성 규칙 강화

- [x] `tests/test_architecture_dependencies.py`의 순수 코어 규칙을 유지한다.
- [x] 새 계약 모듈은 app·API·persistence·설정·네트워크에 의존하지 않도록 검사한다.
- [x] Application에 기능별 contracts·순수 trading_review 의존성을 좁게 허용한다.
- [x] Application의 concrete adapter·FastAPI 의존 금지를 유지한다.
- [x] 조립용 dependency provider와 HTTP handler의 허용 의존성을 구분한다.
- [x] `app/`의 HTTP 인증·오류 처리·운영 CLI를 목록화하고 각 책임의 이동 단계를 정한다.
- [x] 기존 위반은 정확한 파일·import·제거 단계로 관리하고 신규 위반을 차단한다.
- [x] 잘못된 absolute/relative import가 검사에 걸리는지 검증한다.

구체적인 규칙, 초기 31개 legacy import의 제거 단계와 후속 감소 현황, app 역할 목록, 검증 범위는
[AR-01 의존성 운영 문서](ARCHITECTURE_DEPENDENCIES.md)에 기록했다.

전체 `trading_review` 규칙을 완화해 adapter를 넣지 않는다. 새 규칙 때문에 아직
이동하지 않은 코드가 실패한다면 기존 위반만 제한적으로 추적하며, package 전체를 제외하지 않는다.

### AR-02N — Normalization 계약 로직 분리

- [x] identity·result_integrity·result_delivery를 `contracts/normalization/`으로 옮긴다.
- [x] MEDIA_TYPE의 소유권을 동일한 값으로 전달 계약에 옮긴다.
- [x] API·worker·repository·테스트 consumer를 함께 전환하고 이전 세 app 모듈을 제거한다.
- [x] 임시 re-export 없이 AR-02N legacy 예외 11개를 제거한다.
- [x] API schema는 API에, 결과 projection은 기존 위치에 유지한다. Projection 이동은 AR-05다.
- [x] 세 모듈 AST와 고정 입력의 identity·V1/V2 bytes·result hash·요청 JSON schema 동일성을 확인한다.
- [x] Context fixed vector, 잘못된 입력·버전·해시, 바이트 상한, 실 DB publication·reload를 검증한다.
- [x] 전후 contract-sync와 고정 Core의 실제 HTTP·발행·ACK·버전 pin E2E 결과를 확정한다.

경계·비교 결과·후속 이행은 [Normalization 계약 문서](NORMALIZATION_CONTRACTS.md)에 기록한다.
Root package의 eager app bootstrap 분리는 AR-11에서 별도 검토한다.

### AR-02A — Analytics 계약 로직 분리

- [x] `app/trading_analytics_identity.py`와 Analytics의 순수 계약 책임을 `contracts/analytics/`로 옮긴다.
- [x] API·worker·repository consumer를 함께 전환하고 관련 legacy 예외 5개를 제거한다.
- [x] section 정의·digest를 계약으로 옮기고 결과 projection 이동은 AR-05A와 연결한다.
- [x] API schema는 API에 유지하고 이전 identity 모듈과 임시 re-export 없이 consumer를 전환한다.
- [x] Analytics fixed vector, 잘못된 입력·버전·해시와 실 DB publication·payload reload를 검증한다.
- [x] 고정 시각 worker bytes·identity/fingerprint·요청 schema와 추출 전후 AST 동일성을 확인한다.

전후 비교, 직접 영향 범위의 DB 4개 통과, 확장 DB 일괄 검증의 Windows 연결 오류와
미구축 Core E2E는 [Analytics 계약 문서](ANALYTICS_CONTRACTS.md)에 구분해 기록한다.

두 기능의 canonicalizer를 코드가 비슷하다는 이유만으로 합치지 않는다.
계약 분리에서는 필드 기본값, coercion, schema, serializer 동작을 변경하지 않는다.

### AR-03N — Normalization application use case와 port

- [x] 기존 DTO·오류를 보존하고 접수·status·grant·취소·descriptor의 좁은 port를 정의한다.
- [x] 기존 DB 고유 UniqueViolation → application idempotency conflict 매핑을 유지한다.
- [x] API transport 검증·응답은 유지하고 수동 admission 정책과 명령·조회 조율을 application으로 옮긴다.
- [x] 검증 순서·오류 우선순위와 key/fingerprint/input hash·V1/V2 pin을 보존한다.
- [x] Provider에서 repository를 조립하고 다섯 HTTP 경로를 application에 연결한다.
- [x] 작은 command projection과 기존 payload 참조를 사용하며 전체 payload를 중복 복사하지 않는다.
- [x] 정책 테스트·기존 HTTP 회귀·격리 DB 공유 capacity·lock·grant·취소 검증을 수행한다.
- [x] 전후 계약 동기화·전체 suite·실제 Core HTTP·발행·ACK E2E 최종 결과를 확정한다.

공용 payload·ACK의 조율은 AR-04에 남지만 provider를 공유하므로 Normalization concrete
import는 이번 단계에서 제거했다. [경계와 검증 문서](NORMALIZATION_APPLICATION.md)에
검토·커밋 분할안, HTTP transport 전제와 보존한 순서, 남은 범위를 기록한다.

### AR-03A — Analytics application use case와 port

- [x] 접수, status, grant update, cancellation, descriptor 조회의 port를 정의한다.
- [x] 기존 adapter 오류 세 종류를 application 소유로 이동하고 동일 class identity를 사용한다.
- [x] HTTP JSON/헤더·wire 검증·응답은 API에, admission 정책과 명령·조회 조율은 application에 둔다.
- [x] Replay 200·신규 202, capacity·version·grant 우선순위와 단계별 오류 처리 범위를 보존한다.
- [x] Repository 조립은 dependency provider에 두고 Job·전달 consumer에 port를 노출한다.
- [x] 기존 검증된 JSON projection 하나를 사용하며 불필요한 대형 DTO나 payload 복사를 추가하지 않는다.
- [x] 실제 DB에서 공유 capacity·lock·fingerprint 충돌·취소·grant와 HTTP/worker/전달 연결을 검증한다.

범위·커밋 분할안·전후 비교·실 DB 25개 검증과 미구축 Core E2E는
[Analytics application 문서](ANALYTICS_APPLICATION.md)에 기록한다.
공용 payload·ACK 조율은 AR-04에 남으며 worker 본문은 유지한다.

### AR-04 — 공용 terminal payload와 ACK

- [x] 양쪽 Job의 payload 조회와 ACK를 조율하는 application 진입점을 둔다.
- [x] Provider·port 전환으로 Analytics route import와 concrete repository import를 제거한다(AR-03A 선행 완료).
- [x] Payload 탐색, 만료·미존재 오류, media type, encoding, exact bytes를 보존한다.
- [x] PERSISTED/DISCARDED_STALE, token·generation, 동일/상이 ACK replay를 검증한다.
- [x] ACK 후 bytes 정리와 receipt 보존, 24시간 retention 동작을 검증한다.

33개 전후 비교·정책 테스트 27개·전체 로컬 573개·직접 범위 DB 7개·고정 Core publication을
검증했다. 확장 DB·cutover의 환경 오류는 PASS로 처리하지 않고
[공용 전달 application 문서](TERMINAL_DELIVERY_APPLICATION.md)에 분리해 기록한다.

공용 HTTP resource가 있다는 이유로 양쪽 Job의 상태 모델이나 저장 schema를 통합하지 않는다.

### AR-05N — Normalization worker와 실행 adapter

- [x] Worker 조율을 application으로 옮기고 기존 worker repository/executor/metrics port를 사용한다.
- [x] Outcome·계산용 설정과 검증된 입력 변환·계산·실패 분류를 application에 둔다.
- [x] Artifact HTTP와 spawn/IPC·종료를 Normalization technical adapter로 분리한다.
- [x] 공개 결과 projection을 application으로 옮기고 이전 mixed module 경로를 제거한다.
- [x] App은 기존 설정 검증·객체 조립·signal·CLI를 유지한다.
- [x] 고정 V1/V2 bytes·hash, 원래 계산/worker/process AST와 실제 spawn·종료·부모 설정 제외를 확인한다.
- [x] 실제 DB에서 worker 완료·취소·lease 상실·publication 상한과 기존 lease/grant 경계를 검증한다.
- [x] 실제 Core dispatch·child 계산·발행·ACK publication 결과를 확정한다.

범위·커밋 분할안·비교·검증과 기존 EOF join 위험은
[Normalization worker 문서](NORMALIZATION_WORKER_APPLICATION.md)에 기록한다.
EOF 대기와 예외 경로의 자원 회수 보강은 구조 변경과 분리한 후속 운영 PR에서 검토한다.

### AR-05A — Analytics worker와 Artifact adapter

- [x] 실행 조율을 application으로 옮기고 별도 worker repository port와 downloader callable을 사용한다.
- [x] 입력 다운로드·허용 host·redirect·길이·SHA 검증을 technical adapter로 분리한다.
- [x] 입력 검증·Engine 변환, 공개 projection·envelope·bounded JSON을 application으로 옮긴다.
- [x] App entrypoint에 설정·DB·adapter 조립과 signal·CLI만 남기고 이전 mixed module을 제거한다.
- [x] Lease 60초·heartbeat 20초·join 1초, stop 뒤 발행 억제·오류 분류와 실제 DB 연결을 검증한다.
- [x] Console script·systemd·logger 동작과 고정 결과 bytes·hash·요청 schema를 보존한다.

[Analytics worker 문서](ANALYTICS_WORKER_APPLICATION.md)에 비교·검증·실행 한계를 기록한다.
기존 Analytics는 동기 실행이므로 이 단계에서 spawn/IPC·새 격리·timeout을 도입하지 않았다.
AR-08~10 정책·구현·운영 범위와 구분한다. 미구축 Analytics Core publication E2E는 후속 gate로 유지한다.

### AR-05B — Backtest 실행 adapter

- [x] `application/backtest_process_executor.py`의 multiprocessing 구현을 technical adapter로 옮긴다.
- [x] 기존 application port·worker 조립에 연결하고 child import·spawn/IPC·deadline을 검증한다.
- [x] 기존 계산·lease·발행·CLI·systemd 동작을 보존한다.

[Backtest process adapter 문서](BACKTEST_PROCESS_ADAPTER.md)에 AST·실제 spawn 결과 bytes 비교,
전체 626개·실 DB 7개·고정 Core smoke 결과와 남은 운영 위험을 기록한다.
EOF join·예외 시 자원 회수와 child의 전체 Settings 전달 축소는 별도 보강 대상으로 추적한다.

Normalization → Analytics → Backtest 순으로 기능별 PR로 나누며, 각각 production 경로를
완전히 연결한 상태로 머지한다.

### AR-06 — Persistence 내부 책임 분리

Normalization과 Analytics를 별도 기능 PR로 검토한다. 일반적인 load/save repository나
범용 Unit of Work로 치환하지 않는다. Schema migration은 기본 범위에 포함하지 않는다.
필요성이 발견되면 additive migration과 구/신 runtime 호환성을 별도 PR로 설계한다.

#### AR-06N — Normalization

- [x] Row/document projection과 payload 암호화·저장 integrity 검증을 내부 모듈로 분리한다.
- [x] Grant 보조 SQL은 기존 cursor를 받아 실행하고 repository가 transaction을 계속 소유한다.
- [x] 외부 capability·application 오류 매핑과 SQL 61개의 본문·실행 순서를 보존한다.
- [x] 소유권·취소·pin 재검사와 상태·bytes·descriptor·attempt 기록의 원자성을 보존한다.
- [x] Admission·lease·grant·retry·ACK·cleanup·trigger와 공유 capacity를 실 DB로 검증한다.
- [x] 취소/중복 발행 경합, payload INSERT 직후 실패 시 rollback과 이후 재발행을 검증한다.
- [x] 고정 Core dispatch·publication·ACK 재시도·version pin E2E의 최종 통과를 확인한다.

[Normalization persistence 문서](NORMALIZATION_PERSISTENCE.md)에 분할 경계·비교·검증과
환경 실패와 후속 재검증을 기록한다. 임시 IPv6 loopback bootstrap에서 같은 소스로 전체
645개와 Core publication·ACK/ORCH-024~028을 통과했다. 기본 Windows 네트워크 오류와
production 네트워크·부하 검증은 구분하며, 최초 실패 manifest도 보존한다.

#### AR-06A — Analytics

구현·검증을 완료했다. 다음은 한 기능 PR의 검토·커밋 경계다.

1. 순수 accepted/status/descriptor와 저장 request·grant document 변환, AES-GCM codec을
   내부 모듈로 분리하고 고정 bytes·오류 분류 검증을 포함한다.
2. Repository consumer를 연결하고 SQL·transaction·lease predicate를 보존한 채 실제 DB의
   중복 발행·취소 경합·rollback 및 기존 API/worker/ACK 세 모드를 검증한다.

`_db_time`, claim predicate·바인딩, 짧은 accepted 조회 SQL은 repository에 유지한다.
Normalization의 grant SQL 모듈을 그대로 복제하거나 두 기능의 상태 모델을 합치지 않는다.
기존 미구축 Analytics Core publication E2E는 Integration/AR-10의 별도 gate로 유지한다.

- [x] 실제 응집도를 검토해 row/document·암호화·SQL 책임의 분리 범위를 정한다.
- [x] 기존 port·오류 매핑, publication 원자성과 admission·lease·retry·ACK·cleanup 조건을 보존한다.
- [x] SQL·transaction·trigger와 immutable integrity metadata의 전후 보존을 확인한다.
- [x] 만료/교체 claim·중복 claim·취소/발행 경합과 공유 capacity를 실 DB로 검증한다.
- [x] API/worker/전달 연결과 해당 단계의 Core E2E 지원 범위·공백을 확정한다.

[Analytics persistence 문서](ANALYTICS_PERSISTENCE.md)에 12개 전후 bytes·SQL 48개·AST 비교와
전체 663개·실 DB 37개 검증을 기록했다. Analytics Core E2E 미구축 공백은 기존 후속 gate로
유지하며, 임시 loopback 검증 환경과 production 환경을 구분한다.

### AR-07 — Snapshot 조회 경계와 요청 내 재사용

- [x] 변경 전 같은 fixture로 `/assets` 로딩 횟수와 대표 Snapshot 조회 비용을 기록한다.
- [x] Asset/series 조회의 Snapshot 선택·loader port·조립 경계를 명확히 한다.
- [x] `/assets`는 한 번 로딩한 검증된 Calendar에서 다섯 자산의 availability를 계산한다.
- [x] Loader 호출 횟수, pinned Snapshot 일관성, 기존 응답·데이터 오류 처리를 검증한다.
- [x] Ingestion의 artifact → manifest → metadata 발행 순서를 유지한다.
- [x] 동일 fixture로 변경 전후 로딩 비용과 결과를 비교한다.

[Snapshot 조회 검증](SNAPSHOT_QUERIES.md)에 5→1회 로딩·15→3회 읽기, 여섯 HTTP 응답 bytes와
runtime OpenAPI 동일성, legacy 예외 0개, 전체 681개·고정 Core smoke 결과를 기록했다.

전역 캐시, partial artifact loader, 새 저장 포맷은 포함하지 않는다. `/series`의 전체 로딩은
측정 후 별도 최적화 후보로 남긴다. Metadata 등록 실패 후 잔여 artifact 정리는 기존 발행
정책을 확인하며, 조회 리팩토링에 새 삭제 정책을 섞지 않는다.

### AR-08 — Analytics 실행 제한 정책 결정

- [x] Context 정본에 deadline·격리 정책 ADR 초안을 제안한다. ADR-071은 DRAFT이며 승인 대기다.
- [ ] Timeout의 시작/끝, 다운로드·검증·계산·projection·직렬화 포함 범위를 정한다.
- [ ] Timeout 오류 코드, retry 가능 여부·예산, 취소·lease 상실과의 우선순위를 정한다.
- [ ] IPC 메모리·결과 상한, 종료 유예·강제 종료, 지원 OS의 spawn 동작을 검토한다.
- [ ] 새 배포 digest·VersionSet pin·기존 queued/running Job과 rollback 처리를 정한다.
- [ ] Core의 오류 처리 영향과 OpenAPI 변경 필요 여부를 확인한다.

Backtest의 10분 제한이나 EXECUTION_TIMEOUT을 Analytics 기본값으로 추정하지 않는다.
정책이 확정되기 전에는 production 실행 제한을 연결하지 않는다.

[Analytics 실행 정책 검토](ANALYTICS_EXECUTION_POLICY_REVIEW.md)에 현재 구현의 실행·IPC·
메모리 위험, Context ADR-071 제안과 계약·persistence·executor·Core/Integration·운영 전환의
PR 순서를 기록했다. 제안서 작성은 정책 승인이나 운영 수치 확정·실행 코드 완료를 뜻하지 않는다.
AR-08의 나머지 체크박스는 승인·수치 검증 후 갱신한다.

[1차 자원 실측](ANALYTICS_RESOURCE_BASELINE.md)과 개발용 합성 workload·검증을 추가했다.
입력 바이트 외 후보 수·projection 비용·RSS 증폭을 확인했으며, 최대 입력/출력·Linux hard limit·
실제 전송·DB 경합·동시성 실측과 운영 수치 승인은 남아 있다. Production 실행 제한은 미연결이다.

[Linux 후속 실측](ANALYTICS_RESOURCE_LINUX.md)은 현재 main·잠금파일 기반 WSL 회귀 689개와
8개 profile을 검증했다. Production systemd/메모리 격리·최대 허용 입력·동시 worker·실제 Core
E2E gate는 남아 있으며 AR-08~10 완료나 운영 수치 승인으로 해석하지 않는다.

### AR-09 / AR-10 — Analytics 격리 준비와 production 연결

- [ ] AR-09: executor·입력/outcome을 구현하고 정상·프로세스 crash·IPC 오류·큰 결과를 검증한다.
- [ ] AR-09: 강제 중단 가능한 timeout·취소·lease 상실 시나리오를 검증한다.
- [ ] AR-10: worker에 연결하고 부모만 heartbeat·조건부 발행·실패 처리를 수행하게 한다.
- [ ] AR-10: 늦게 반환한 결과가 취소·상실된 claim을 발행하지 못하는지 검증한다.
- [ ] AR-10: 설정·systemd·메트릭·runbook과 격리 DB/Core 경로 검증을 함께 연결한다.

준비 PR의 executor는 기존 production 실행 경로를 바꾸지 않으며, 연결 PR 전까지
미연결 상태임을 본문에 명시한다. Normalization·Backtest의 retry 정책은 유지한다.

### AR-11 — 최종 정리와 선택적 후속 작업

Analytics 결과 구성의 scalar 반복 변환·전체 필터링은
[호출 내 unit index 재사용](ANALYTICS_PROJECTION_SCALARS.md)으로 별도 개선·검증했다.
이 작업은 AR-08 정책 승인이나 AR-09/10 실행 제한·실제 Core E2E 완료를 뜻하지 않는다.

- [ ] 사용하지 않는 re-export·이전 경로·테스트의 import monkeypatch 경로를 정리한다.
- [ ] `app/`의 조립 외 책임, application의 concrete adapter 의존, persistence의 app 역참조를 검사한다.
- [x] AR-11A: HTTP 인증을 inbound `api/security.py`로 옮기고 이전 app 경로·route 예외를 제거한다.
- [ ] Ingestion·cleanup·benchmark CLI의 업무 조율은 해당 기능에 둔다.
- [ ] 남은 실질적 구현 이동은 기능별 PR로 분리하며 최종 정리 PR에 몰지 않는다.
- [ ] 실제 프로세스·의존 방향·계약 ownership을 `docs/ARCHITECTURE.md`와 runbook에 반영한다.
- [ ] 남은 정책 공백·미검증 시나리오·지원하지 않는 조합을 기록한다.

[HTTP 인증 경계 정리](HTTP_AUTH_BOUNDARY.md)는 17개 계약 operation의 인증 응답과
runtime OpenAPI·설정/오류 처리·공개 probe를 보존한다. App의 오류 handler·CLI 업무 조율과
최종 architecture/runbook 전체 정리는 후속 개별 범위이며 AR-11 전체를 완료로 표시하지 않는다.

다음 항목은 별도 후속 후보이며 기본 구조 개선의 완료를 가로막지 않는다.

- [ ] Temporal profile에서 반복 탐색이 병목으로 확인되면 자산별 index·completion 이진 탐색을 검토한다.
- [ ] 최적화 시 동일성·cross-calendar·missing-session regression과 전후 benchmark를 함께 검증한다.
- [ ] 실제 탐색 비용이 남을 때 feature 전체 패키지 이동을 별도 설계한다.
- [ ] 전역 캐시는 중첩 불변성·메모리 상한·eviction을 명시할 수 있을 때 검토한다.

## 5. 검증과 커밋 운영

PR별 기본 검사는 다음과 같다. 프로젝트 가상환경을 사용한다.

```powershell
.venv/Scripts/ruff.exe check src tests
.venv/Scripts/pyright.exe --pythonpath .venv/Scripts/python.exe
.venv/Scripts/python.exe -m pytest -q
git diff --check
```

변경 파일의 formatter 검사와 관련 unit/contract 테스트를 우선 실행한다.
DB 변경은 `tests/integration/test_postgres_{backtest,trading_review,trading_analytics}_jobs.py`
중 영향 범위를 실제 실행하고 skip을 통과로 해석하지 않는다. 격리 DB 구성·migration은
현재 schema 요구사항을 확인하며 오래된 test docstring의 migration 번호에만 의존하지 않는다.

Core↔Compute 호출에 영향을 주는 변경은 구현 전후 contract-sync를 실행한다.
Compute 동작 변경의 smoke는 관련 Core dispatch부터 terminal까지 검증한다.
기동·credential·격리 DB·fixture·evidence의 정본은
[`../../integration/README.md`](../../../integration/README.md)와
[`../../integration/AGENTS.md`](../../../integration/AGENTS.md)이며 여기서 복제하지 않는다.
기존 Normalization runtime/publication/version-pin harness를 영향 범위에 맞게 선택한다.
Analytics fixture 검사를 Core runtime E2E 완료로 해석하지 않으며, 필요한 harness가
없으면 Integration 소유의 후속 작업으로 명시한다.

커밋 하나에는 설명 가능한 변경과 관련 검증을 함께 담는다. 위 단계의 범위가 커지면
동일 기능의 독립 capability로 더 나누되, 테스트·문서만 기계적으로 떼지 않는다.
예시는 `refactor(trading-review): extract normalization integrity contract`,
`refactor(analytics): introduce job application ports`,
`perf(marketdata): reuse verified snapshot within asset query`다.
각 PR에 선행 단계, 미연결 경계, 실행한 검사, API/hash/DB/runtime 영향, rollback 조건을 적는다.
`PR_BODY.md`, credential, 로컬 fixture·evidence는 add/commit/push하지 않는다.

## 6. 완료 판정

구조 개선은 AR-00~07 및 해당 AR-11 정리가 완료되고 기존 동작·계약·원자성이 보존되면
완료로 판정한다. Analytics deadline 운영 개선은 AR-08~10과 해당 운영 문서·검증을
별도로 완료 처리한다. 선택적 Temporal 최적화와 전체 패키지 이동은 별도 후속 작업이다.
전체 테스트 통과만으로 DB 경쟁·Core E2E·메모리 상한·crash recovery 검증을 대체하지 않는다.

현재 문서 작성의 Determinism impact: none. Point-in-Time impact: none.
실제 구현 PR은 동일성 검증 결과와 남은 미검증 범위를 각각 기록한다.
