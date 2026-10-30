# Backtest 프로세스 실행 adapter — AR-05B

상태: AR-05B 완료. 구조 전환·로컬 회귀·실 PostgreSQL·고정 Core smoke 검증을 마쳤다.
검증 run: `20261029T104933Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 검토·커밋 경계

AR-05B는 Backtest 실행 구현 한 기능 PR로 검토한다.

1. Process executor와 child outcome 송신 adapter를 추가하고 실제 spawn·EOF·deadline·종료,
   picklable factory 및 안전한 실패 분류를 검증한다. 준비 단계에서는 기존 연결을 유지할 수 있다.
2. 기존 worker 조립·consumer를 새 adapter로 전환하고 이전 application 구현을 제거한다.
   실제 DB worker·Core dispatch/terminal smoke와 전체 회귀 검증을 함께 확인한다.

선행 작업과 PR 보조 파일은 보존한다. 이 문서의 완료 표시는 commit·push·배포 완료를 뜻하지 않는다.
Normalization/Analytics persistence 내부 책임 분리는 후속 AR-06에서 기능별로 진행한다.

## 2. 책임과 연결

| 위치 | 책임 |
|---|---|
| application/backtest_worker_host.py | 기존 executor port·outcome, lease·heartbeat·retry·DB 발행 조율 |
| application/backtest_workers.py | 기존 claim·pinned Snapshot 조회·bounded retry 정책 |
| application/backtest_executor.py | 기존 검증된 입력에서 Daily Bar 계산·결과 변환 |
| adapters/backtest/process_executor.py | spawn·Pipe·deadline·poll/join·terminate/kill, child outcome 송신·close |
| app/worker.py | 기존 설정 검증·부모 DB/관측 조립, child용 계산기 factory, signal·CLI |

기존 `application/backtest_process_executor.py`는 제거하고 app과 테스트 consumer를 전환했다.
임시 re-export module은 두지 않는다. 기존 BacktestJobExecutor port와 outcome 타입을 그대로
사용하며 범용 worker framework나 새 계약 DTO는 추가하지 않는다.

Top-level `execute_backtest_child`는 picklable executor factory와 ExecutableBacktestJob을 받는다.
App의 `_create_daily_bar_executor`가 child 안에서 Snapshot loader와 계산기를 조립한다.
부모에서 열린 DB connection·SDK client·계산기를 pickle해 넘기지 않는다. Child는 outcome만
반환하고 lease를 소유한 부모가 기존 repository를 통해 terminal 상태를 기록한다.

Adapter는 app/API/persistence/Engine/네트워크를 직접 import하지 않고 기존 application port만
참조한다. Application 전체의 multiprocessing·subprocess import를 차단했으며, 기존 legacy
예외는 assets/series의 6개만 남는다. Factory callable을 통한 조립은 정적 import 검사만으로
완전히 추적하지 않으므로 실제 spawn·DB·E2E 검증을 함께 사용한다.

## 3. 동작 보존과 비교

ADR-040/041/042/044와 OpenAPI를 기준으로 기존 동작을 보존한다.

- 기본 execution deadline 600초, 실제 deadline 설정 지점·0.1초 poll·결과 수신 후 남은 시간 join을 유지한다.
- Deadline 초과 시 terminate·30초 join·필요 시 kill·join 후 EXECUTION_TIMEOUT을 반환한다.
- Child가 결과를 보내도 deadline 안에 종료하지 않으면 timeout으로 처리한다.
- EOF·outcome 없는 종료는 기존 RetryableBacktestFailure로 처리한다.
- Child 계산기 조립/실행·첫 송신 실패의 안전한 retryable outcome과 sender close를 유지한다.
- 부모 lease 60초·heartbeat 15초, retry 간격·발행·Snapshot pin·cleanup 정책은 변경하지 않는다.
- Console script·systemd·환경 변수·metrics/event·기존 logger 이름을 유지한다.

Process executor class AST는 이동 전과 동일하다. Child wrapper는 계산기 생성 표현을 factory
호출로 정규화한 AST가 같고, 생성 표현 자체와 기존 app의 signal/CLI/run 함수 AST도 동일하다.
다른 production source·SQL·migration·OpenAPI·fixed fixture·deploy 파일은 이번 단계에서
변경하지 않았다. Determinism impact: none. Point-in-Time / Execution semantics impact: none.

실제 app 조립과 spawn으로 고정 입력을 실행한 전후 비교:

| 사례 | 결과 | 바이트 | SHA-256 |
|---|---|---:|---|
| Manifest-backed zero trades | CompletedBacktestExecution | 827 | `ee0448d9b87573ac4710b870101dfb230aaadb4ede80a9db1dc26c2f78a698b9` |
| 잘못된 저장 payload | TerminalBacktestFailure | 96 | `d21a71ab3e290b585e8e8c528c294a37dbe176f2f0ddc4f96b822a2352f25b9a` |

표의 bytes는 결과 outcome을 고정 JSON으로 직렬화한 비교 산출물이다. HTTP envelope와 혼동하지
않는다. ID·Snapshot·engine version을 고정했으며 두 실행의 전체 bytes가 일치했다.

## 4. 검증

- 직접 영향 회귀 90 passed: process·entrypoint·worker host·Daily Bar·의존성 검사.
- 전체 로컬 suite 626 passed, 125 skipped: DB URL 없는 38개와 폐기된 PoC 87개는 skip이다.
- 실제 PostgreSQL Backtest suite 7 passed, skip 없음. 새 schema에 0001~0017을 적용했다.
- Formatter 245 files·Ruff·Pyright strict, 전후 계약 동기화 검사 통과.

기존 결과 수신 후 child가 멈추는 테스트는 100ms가 지나 spawn 완료 전에 timeout될 수 있었다.
Child가 결과를 전송했다는 marker를 확인하고 충분한 시작 시간을 주어, 결과 수신 뒤에도
종료 deadline을 지키는 실제 경로를 검증하도록 보강했다. EOF·강제 종료·factory pickle·실제
Snapshot 계산·IPC 실패 분류도 확인했다.

실 DB에서는 exclusive claim/reclaim, idempotency·capacity, lease 소유자 발행·cleanup과
manifest-backed zero trade 완료를 검증했다. 마지막 사례는 기존 직접 executor와 새 adapter를
사용한 production worker 조립 두 경로를 모두 실행한다. Core smoke의 최초 실행은 migration
준비 중 Windows Bad address 10014로 중단됐다. 재실행은 네 성공 사례 검사를 지난 뒤 마지막
실패 사례 polling에서 로컬 연결 오류로 중단됐다. 두 실패 로그를 보존하고 준비된 전용 schema와
immutable fixture를 사용하는 새 프로세스에서 전체 smoke를 다시 실행했다. Core 기동 중의
Windows socket listen 오류도 기존 harness의 소유 프로세스 재시도로 복구됐으며 증적에 남겼다.

최종 Core smoke는 종료 코드 0이다. Same-market·cross-market·drawdown·drawdown replay의
COMPLETED, 의도한 Calendar 오류의 FAILED와 구조화된 오류 코드 보존, Core의 결과 저장·조회,
deterministic replay·latest 결과 조회·다른 member의 접근 거절까지 기존 harness 검사를 통과했다.
이전 baseline과 같은 고정 Core `206473c6e7c5214d23c731935e9ead703507a2b6`를 사용했다.
원래 Core checkout의 현재 HEAD와의 통합을 검증했다는 뜻은 아니다. 이번 실행이 소유한
API·worker 프로세스 registry가 모두 비었는지도 확인했다.

## 5. 남은 운영 위험

기존 EOF 분기의 무제한 join, process 시작/IPC 예외 경로의 자원 회수, heartbeat 종료 대기 등은
이번 이동에서 변경하지 않았다. 별도의 운영 보강 PR에서 deadline·lease·종료 경합을 검증하며
개선할 대상이다. 새 adapter 경계만으로 이 위험이 해소됐다고 보지 않는다.

기존 Settings는 factory partial을 통해 계속 child로 전달된다. DB URL 등 불필요한 필드를
줄이는 작업과 package root의 eager app bootstrap 분리는 별도 보강 범위다. 부모만 DB 상태를
변경한다는 실행 규칙은 보존했지만 OS 환경이나 child의 credential 접근을 격리한 것은 아니다.
새 배포 artifact/digest 등록과 운영 배포는 검증 범위 밖이다.

로그·JUnit·전후 outcome·AST 비교·source fingerprint·checksum은
[AR-05B manifest](../../../integration/.integration-data/architecture-refactoring/ar05b/20261029T104933Z/manifest.json)에
보관한다. Credential·로컬 runtime env·fixture 산출물은 Compute에 커밋하지 않는다.
