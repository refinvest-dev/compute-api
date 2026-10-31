# AR-01 의존성 규칙과 이행 목록

상태: AR-01 구현. Runtime·API·계산 정책을 변경하지 않고 정적 import 검사를 강화한다.
정책·계약의 정본은 Context이고, 이 문서는 Compute 로컬 코드의 의존성 운영 기준이다.

실행 검사는 [test_architecture_dependencies.py](../tests/test_architecture_dependencies.py),
현재 이행 예외의 정본은 [architecture_legacy_imports.json](../tests/architecture_legacy_imports.json)이다.
기존 [Trading Review runtime 검사](../tests/test_trading_review_runtime_architecture.py)도 유지한다.

## 1. 허용 경계

AR-05N은 Normalization technical adapter의 app/API/DB 의존을 차단한다. Artifact HTTP와
process 구현은 `adapters/normalization`에, 계산·worker 조율은 application에 둔다.
순수 `application/normalization_execution.py`의 multiprocessing·subprocess·pathlib도 차단한다.
AR-05A는 Analytics HTTP adapter의 app/API/DB/worker 의존도 차단한다. 순수 입력 변환과
결과 projection의 multiprocessing·subprocess·pathlib도 차단하며 application의 네트워크
금지는 유지한다. AR-05B에서는 Backtest spawn·IPC·deadline·종료 구현을
`adapters/backtest`로 옮겼다. Application 전체의 multiprocessing·subprocess import를
차단하고 Backtest adapter의 app/API/DB/직접 네트워크 의존도 금지한다.

| Source | 허용되는 경계 | 차단하는 대표 의존성 |
|---|---|---|
| 모든 production package | 자신의 역할에 맞는 package | tests, integration, Core/Web source import |
| package root __init__ | 표준 라이브러리, 호출 시 app.main.main 진입점 | FastAPI app 재수출, app.config·외부 framework 직접 import |
| engine | 표준 라이브러리, engine, marketdata.domain/ports/errors | FastAPI·DB·SDK, app/application, ingestion, marketdata adapter |
| trading_review | 표준 라이브러리, trading_review | 다른 refinvest 계층, 외부 SDK |
| marketdata.domain/errors | 표준 라이브러리와 자신 | application, adapter, SDK |
| marketdata.ports | 표준 라이브러리, marketdata.domain/ports/errors | concrete adapter·application |
| application | 표준 라이브러리, application, engine, 순수 trading_review, marketdata 인터페이스, 승인된 contracts | FastAPI·DB·Vendor SDK, app·API·persistence·ingestion·marketdata adapter |
| adapters.backtest | 표준 라이브러리, 자신의 package, application.backtest_worker_host port/outcome | app·API·persistence·Engine·네트워크·외부 SDK |
| adapters.analytics | 표준 라이브러리, 자신의 package, application.analytics_input, contracts.analytics | app·API·persistence·worker 구현·외부 SDK |
| contracts | 표준 라이브러리, contracts.normalization/analytics, Pydantic, 순수 trading_review.versions | app·API·application·persistence·ingestion·engine, 설정·네트워크·프로세스·파일시스템 구현 |
| api.routes | FastAPI/Starlette, schema·dependency provider, application, 계약·시장데이터 인터페이스, 관측·인증·설정의 명시적 경계 | concrete persistence, ingestion, 다른 route, 그 외 app 구현 |
| api.dependencies | 외부 구현체와 설정을 조립 가능 | route import, 테스트·타 서비스 source |
| api.security | 표준 라이브러리, FastAPI, app.config의 Settings/get_settings | application·DB·route·dependency provider·네트워크·다른 app 구현 |
| api.exception_handlers | 표준 라이브러리, FastAPI/Starlette, api.schemas.errors | app·application·DB·route·dependency provider·네트워크 |
| api.http_metrics | 표준 라이브러리, FastAPI/Starlette, observability.metrics의 ApiMetrics/safe_record | app·application·DB·route·dependency provider·직접 네트워크·listener 조립 |
| api.schemas | Pydantic/FastAPI, schema, 계약, engine.validation 타입 | app, persistence, ingestion, route, dependency provider |
| persistence | DB·Artifact SDK, port·순수 모델·계약 | app·API 역참조, 테스트·타 서비스 source |

표준 라이브러리 허용만으로 순수성이 보장되지는 않는다. engine·trading_review·application·contracts는
socket/urllib/http 등의 직접 네트워크 import와 os 설정 접근을 추가로 차단한다.
contracts는 pathlib·subprocess·multiprocessing도 차단한다. 계약의 메모리 내 JSON/bytes 처리와
Pydantic 검증은 허용한다. 정규화 identity의 기존 정확한 지원 registry 참조를 위해
`trading_review.versions`만 승인하며, 결과 projection을 계약 모듈에 넣는 근거로 사용하지 않는다.

Application은 `contracts.normalization`, `contracts.analytics` 두 경계만 승인한다.
임의의 미래 contracts 하위 package를 자동 승인하지 않는다. AR-01에서는 synthetic source로
규칙을 검증했고, AR-02N/02A에서 실제 normalization·analytics 계약 모듈을 연결했다.

HTTP 경계의 `StrategyValidationError`, `ScaledDecimal`, `TradingReviewError`는 정확한
타입 import만 허용한다. 이를 계산 pipeline이나 domain service 호출의 승인으로 해석하지 않는다.
Route의 설정 접근은 `app.config`, HTTP 인증은 `api.security`로 명시적으로 허용한다.
AR-11A에서 인증을 inbound 경계로 이동하고 `app.security` 허용을 제거했다.
인증 모듈도 FastAPI와 최소 설정 외 concrete adapter·use case·route·provider에 의존하지 못한다.

AR-11B에서 전역 HTTP 오류 응답을 `api.exception_handlers`로 옮겼다. 이 모듈은 runtime 설정이나
조립에 역참조하지 않으며 `app.main`은 등록만 수행한다. App이 다시 API schema mapper와
FastAPI JSONResponse를 직접 사용해 오류 응답을 구성하는 경로도 검사한다.

AR-11C에서 HTTP 관측 middleware와 label 분류를 `api.http_metrics`로 옮겼다.
Enabled 설정은 app이 콜백으로 전달한다. API 모듈은 설정·listener를 조립하지 않으며
기존 관측 모듈의 `ApiMetrics`와 `safe_record`만 사용한다. App의 `monotonic`·`safe_record`
직접 import도 차단한다. 이는 명시적 import 규칙이며 모든 간접 호출을 탐지하는 검사는 아니다.

AR-11D에서 package root의 즉시 HTTP composition import를 제거했다. 정적 검사는
`app.main.main` 심볼만 허용하고, 별도 Python 프로세스의 import blocker가 root·계산·worker와
모든 console 진입점 로딩의 HTTP composition 재유입을 확인한다. 함수 내부 import와 module-level
호출의 차이를 정적 allowlist만으로 보장한다고 해석하지 않는다. Benchmark의 기존 API DTO 의존은
이번 범위 밖이며 모든 CLI가 FastAPI 자체에 독립적이라고 주장하지 않는다.

Dependency provider는 객체 조립을 위해 concrete adapter를 import할 수 있다.
같은 import를 새로운 HTTP handler에 추가하면 실패한다. 함수가 provider 역할이라는 이유로
route 파일 전체를 면제하지 않는다. AR-07에서 마지막 조회 조립도 provider로 옮겼다.

## 2. 현재 이행 예외

AR-01 시작 시점의 예외는 7개 파일, 18개 import group, 31개 심볼 의존성이다.
AR-02N에서 6개 group·11개 심볼, AR-02A에서 2개 group·5개 심볼을 제거했다.
AR-03N에서 Normalization provider를 옮겨 concrete repository 심볼 1개도 제거했다.
AR-03A에서 Analytics port·오류 소유권·provider 참조를 전환해 3개 group·8개 심볼을 제거했다.
AR-07에서 assets/series의 조립 함수·concrete repository/loader·storage 오류 참조 6개를
모두 제거했다. 현재 registry는 빈 배열이며 production은 legacy 예외 없이 검사를 통과한다.
Snapshot metadata와 loader는 application port를 사용하고 artifact 오류도 loader port가 소유한다.

Normalization job handler는 AR-03N에서 application과 좁은 repository port를 사용하도록
연결했다. 공용 payload·ACK도 새 provider가 반환하는 기존 port를 사용하므로 Normalization
concrete import는 AR-04보다 먼저 제거했다. AR-03A에서도 공용 handler가 새 provider와
Analytics 전달 port를 사용해 관련 concrete·route import를 제거했다. AR-04에서는 공용
payload·ACK의 기능 선택과 조율도 application으로 옮겼다. 의존성 예외 수는 늘리지 않았다.
AR-02의 import 위치 변경과 AR-03의 use case 분리를 구분한다.

예외의 key는 POSIX 상대 파일 경로와 실제 fully qualified import 심볼의 정확한 쌍이다.
JSON에는 원래 module, 심볼 목록, 이유, 제거할 AR 단계를 명시한다.

- 파일 glob, module prefix 면제, wildcard 심볼, 중복 key, 불명확한 제거 단계는 거절한다.
- 동일한 심볼도 다른 파일에서 import하면 새 위반이다.
- 같은 파일에서 새 심볼·하위 모듈을 import하면 새 위반이다.
- 의존성을 제거하거나 허용 경계로 이동한 뒤 예외만 남으면 stale exception으로 실패한다.
- 후속 PR은 코드 전환과 해당 예외 제거를 함께 수행한다. 새 예외로 검사를 통과시키지 않는다.

## 3. app 역할 목록

AR-01 시점 app의 책임 분류다. AR-02N/02A에서 Normalization 계약·MEDIA_TYPE과
Analytics identity·digest 이동을 완료했다. 아래 원래 module 이름은 책임 추적용으로 남기며,
AR-05N에서는 Normalization worker·입력 변환·projection·technical adapter 분리도 완료했다.
AR-05A에서는 Analytics worker·입력 변환·projection·Artifact adapter 분리도 완료했다.
AR-05B에서 Backtest process adapter 이동과 worker 조립 전환도 완료했다.

| 현재 모듈 | 현재 책임 | 목표 경계·단계 |
|---|---|---|
| config.py | 환경 설정과 payload key 해석 | Composition 설정으로 유지; secret 처리는 domain에 전달하지 않음 |
| main.py | FastAPI·handler·middleware 조립, metrics registry/listener 수명 관리 | 전역 오류는 api.exception_handlers, HTTP 관측은 api.http_metrics로 이동 완료; AR-11B/C |
| security.py (제거됨) | 내부 API key HTTP 인증 | api/security.py로 이동 완료; AR-11A |
| worker.py | Backtest 부모 조립·child 계산기 factory·시작·종료 | spawn/IPC/종료는 adapters.backtest로 이동 완료; AR-05B |
| trading_review_worker.py | 설정 검증·조립·signal·CLI | Worker application과 process adapter 연결 완료; AR-05N |
| trading_analytics_worker.py | 설정 검증·DB/adapter 조립·signal·CLI | application worker와 Artifact adapter 연결 완료; AR-05A. 새 격리·deadline은 AR-08~10 |
| trading_review_identity.py | 정규화 identity DTO·hash·지원 registry 확인 | contracts.normalization.identity 이동 완료; AR-02N |
| trading_review_result_integrity.py | V1/V2 public result 검증·canonicalization | contracts.normalization.result_integrity 이동 완료; AR-02N |
| trading_review_result_delivery.py | pinned 전달 policy·바이트 상한 | contracts.normalization.result_delivery 이동 완료; AR-02N |
| trading_review_payload.py (제거됨) | public result projection | application.normalization_payload 이동 완료; AR-05N |
| trading_review_normalization.py (제거됨) | 입력 해석·변환, 다운로드·process 실행 | application.normalization_execution과 adapters.normalization 이동 완료; AR-05N |
| trading_analytics_identity.py | Analytics identity·VersionSet·canonical JSON | contracts.analytics.identity 이동 완료; AR-02A |
| trading_analytics_input.py (제거됨) | Artifact I/O·검증과 계산 입력 변환 | application.analytics_input과 adapters.analytics 이동 완료; AR-05A |
| trading_analytics_result.py (제거됨) | 세 Metric의 public projection | application.analytics_result 이동 완료; AR-05A |
| backtest_job_cleanup.py | cleanup CLI·DB 조립·운영 출력 | 얇은 진입점 유지, 조율 책임 정리 AR-06/11 |
| trading_review_cleanup.py, trading_analytics_cleanup.py | 각 기능 cleanup CLI·조립 | 기능별 정책 유지, 조율 분리 AR-06/11 |
| snapshot_storage.py | Filesystem/S3 factory와 storage 설정 해석 | 조립 유지, 조회 provider 연결 완료; AR-07 |
| snapshot_ingestion.py | CLI 입력, Vendor/발행 구성과 ingestion 조율 | 얇은 CLI와 ingestion application 구분; AR-11 기능별 PR |
| integration_fixture.py, reference_data_smoke.py | 합성 fixture 구성·발행과 참조 데이터 진단 CLI | 검증 도구 책임 유지, 필요시 feature별 구성 분리 AR-11 |
| benchmark.py | benchmark CLI와 기존 benchmarking 모듈 조립 | 얇은 composition 진입점 유지; AR-11 최종 확인 |
| refinvest/__init__.py (package root) | FastAPI app/main eager export | 기존 진입점 호환성을 보존하며 bootstrap 분리 검토; AR-11 |

`__init__.py`는 package 초기화로 유지한다. AR-11에서 남은 실질적인 구현 이동을 발견하면
한 번의 정리 PR에 몰지 않고 해당 기능의 독립 PR로 나눈다.

## 4. 검증 범위와 한계

검사는 production 파일을 import하거나 실행하지 않고 AST를 읽는다.
Absolute/relative import, package `__init__`, alias, `from ... import *`, 중첩·TYPE_CHECKING
안의 import를 검사한다. Package 경계는 점 단위로 비교하므로 `domain_extra`를 `domain`으로
허용하지 않는다. 겹치는 규칙도 함께 적용해 global 금지가 상세 규칙에 가려지지 않게 한다.

정적 import 검사는 동적 import 문자열, re-export의 심볼 기원, 기존 import의 함수 호출
행위나 I/O·트랜잭션 순수성을 완전히 추적하지 않는다. 특히 이미 승인된 심볼을 같은 파일의
새 코드가 재사용하는 행위는 새 import edge가 아니다. 코드 리뷰와 기존 계산·Job regression을
함께 사용하며, 이 검사를 runtime 정확성이나 정보보호 검증으로 확대 해석하지 않는다.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_architecture_dependencies.py tests/test_trading_review_runtime_architecture.py --tb=short
```

AR-01은 규칙·회귀 테스트·이행 목록을 함께 검토하는 변경 범위다. Runtime 전환은 AR-02
이후 기능별 PR에서 수행하며, 이 작업에서는 production import 경로를 변경하지 않는다.
Determinism impact: none. Point-in-Time impact: none.

검증 결과:

- 의존성·기존 Trading Review runtime 검사: 45 passed.
- 전체 로컬 suite: 504 passed, 117 skipped.
- Skip은 DB URL 미설정 30개와 폐기된 test-only PoC 87개로 구분한다.
- Formatter·Ruff lint·Pyright strict 통과.
- Production Python 파일은 AR-00 포맷 보완 이후 바이트와 동일한지 확인했다.

이번 작업은 테스트·검사 자료·문서만 변경하므로 DB·Core E2E를 재실행하지 않았다.
AR-00의 실 DB·Core 검증 기록은 별도 baseline으로 유지한다.

이 절의 수치는 AR-01 실행 기록이다. Production import가 전환된 별도 검증은
[Normalization 계약 문서](NORMALIZATION_CONTRACTS.md)와
[Analytics 계약 문서](ANALYTICS_CONTRACTS.md)에 기록한다.
AR-03N의 HTTP/application 연결과 검증은 [Normalization application](NORMALIZATION_APPLICATION.md)에 기록한다.
AR-03A의 Analytics port·오류·HTTP 연결과 검증은 [Analytics application](ANALYTICS_APPLICATION.md)에 기록한다.
AR-04의 조회·ACK 조율과 검증은 [공용 전달 application](TERMINAL_DELIVERY_APPLICATION.md)에 기록한다.
AR-05N의 worker·child 분리와 검증은 [Normalization worker](NORMALIZATION_WORKER_APPLICATION.md)에 기록한다.

AR-05A의 worker·Artifact 분리와 검증은 [Analytics worker](ANALYTICS_WORKER_APPLICATION.md)에 기록한다.

AR-05B의 process adapter·child 조립 전환과 검증은 [Backtest process adapter](BACKTEST_PROCESS_ADAPTER.md)에 기록한다.

AR-06N은 Normalization row/document 변환, payload 저장 codec과 cursor 기반 grant SQL을
분리한다. Row 모듈은 DB client·암호화를, payload 모듈은 DB client와 repository 역참조를,
grant 모듈은 새 connection·repository 역참조를 차단한다. Repository가 transaction을 계속
소유하며 예외 수는 6개로 같다. [Normalization persistence](NORMALIZATION_PERSISTENCE.md)에
AST·SQL 비교와 DB 경합·rollback 검증을 기록한다.

AR-06N의 후속 단일 회귀·Core publication/ACK·version pin 검증은 [Normalization persistence](NORMALIZATION_PERSISTENCE.md)의 재검증 절을 참고한다. 임시 네트워크 bootstrap은 production 의존성에 포함하지 않는다.

AR-06A는 Analytics persisted document와 AES-GCM codec을 분리한다. Document 모듈의 DB client·
암호화·네트워크, codec의 DB client·repository 역참조를 차단한다. Claim predicate·DB clock·
transaction은 repository에 유지한다. 예외는 6개로 같다. 전후 비교와 실제 DB 경합·rollback은
[Analytics persistence](ANALYTICS_PERSISTENCE.md)에 기록한다.

AR-07에서 마지막 legacy 예외 6개를 제거했다. `/assets`는 요청마다 선택한 Snapshot을 한 번
검증한 뒤 같은 Calendar로 다섯 availability를 계산한다. 요청 사이에는 데이터를 보관하지
않는다. [Snapshot 조회](SNAPSHOT_QUERIES.md)에 전후 호출 수·HTTP bytes·오류와 검증 기록을 둔다.
