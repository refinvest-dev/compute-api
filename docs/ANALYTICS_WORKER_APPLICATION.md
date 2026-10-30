# Analytics worker·Artifact adapter — AR-05A

상태: AR-05A 완료. 구조 전환·고정 결과 비교·로컬 회귀·실 PostgreSQL 검증을 마쳤다.
검증 run: `20261029T101841Z` (UTC), 작업일 2026-10-29 KST.

## 1. 범위와 검토·커밋 경계

한 Analytics 기능 PR에서 다음 경계로 검토한다. 각 준비 단계는 기존 production 경로를
유지하고, 마지막 연결 단계에서 canonical module로 모든 consumer를 전환한다.

1. 순수 입력 검증·Engine 입력 변환, 결과 projection·bounded JSON 직렬화와 고정 vector 검증.
2. Artifact HTTP adapter, 좁은 worker 설정·repository port·다운로드 callable, worker 정책 검증.
3. CLI 조립 연결·consumer 전환·이전 mixed module 제거, 실제 DB HTTP/worker/ACK와 문서.

기존 선행 변경과 PR 보조 파일은 보존했다. 완료 표시는 commit·push·배포 완료를 뜻하지 않는다.
Backtest process adapter는 다음 AR-05B에서 별도로 다룬다.

## 2. 책임과 의존성

| 위치 | 책임 |
|---|---|
| app/trading_analytics_worker.py | 기존 설정 검증·DB/key/adapter 조립·signal·CLI |
| application/analytics_worker.py | claim·heartbeat·검증/계산/발행 순서·오류 분류·종료 확인 |
| application/analytics_jobs.py | 기존 API port와 별도의 AnalyticsWorkerRepository port |
| application/analytics_input.py | immutable 입력 검증·Engine 입력 변환·입력 오류 타입 |
| application/analytics_result.py | 세 Metric의 공개 projection·terminal envelope·bounded UTF-8 JSON |
| adapters/analytics/artifacts.py | host allowlist·redirect·expiry·media/encoding·길이·SHA 검사와 HTTP 접근 |

Worker는 repository Protocol과 downloader callable을 받는다. Application은 Settings,
PostgreSQL, HTTP opener, concrete adapter를 import하지 않는다. Adapter는 순수 입력 helper와
Analytics 계약만 참조하며 app/API/DB/worker 구현을 import하지 않는다. 정적 의존성 검사에
이 경계를 추가했고 기존 legacy 예외는 assets/series의 6개만 남는다.

Frozen AnalyticsWorkerSettings에는 artifact hosts, implementation digest, 입력 바이트 상한
세 필드만 전달한다. DB URL·암호화 key·내부 API key는 worker 설정 DTO에 포함하지 않는다.
같은 프로세스에서 repository를 사용하므로 별도 프로세스 보안 격리를 의미하지 않는다.

`app/trading_analytics_input.py`, `app/trading_analytics_result.py`는 제거했다. Runtime과 테스트는
새 소유 모듈을 직접 import한다. `refinvest-trading-analytics-worker` console entrypoint와
systemd·환경 변수, logger 이름, signal·poll 동작은 유지한다.

## 3. 보존한 동작과 비교

VersionSet 검사 → 두 artifact 다운로드 → 입력 검증·변환 → Engine → projection → section
digest → envelope → bounded JSON → 조건부 publication 순서를 보존한다. 다운로드의 chunk
단위 expiry/stop 검사와 exact bytes 검증, redirect 금지, 안전한 입력 오류 분류도 같다.

- Claim lease 60초, heartbeat 간격 20초·갱신 lease 60초, finally의 heartbeat join 1초를 유지한다.
- 시작 전 shutdown이면 claim하지 않는다. 다운로드·계산 후와 publication 직전 stop을 확인한다.
- Lease 상실·heartbeat 예외·shutdown 이후 성공/실패 terminal write를 억제한다.
- Input failure의 retryable, contract failure의 non-retryable, Engine ValueError 분류,
  일반 예외의 TEMPORARY_DEPENDENCY_FAILURE와 publication conflict 처리를 유지한다.
- 요청과 repository 중 작은 payload 상한을 사용하며 UTF-8 byte 수로 측정한다.
- PostgreSQL 구현·SQL·migration·OpenAPI·Context fixed vector·deploy 설정은 이번 단계에서 변경하지 않았다.

입력 helper 세 개의 공개 이름 변경 외 순수 입력/HTTP 함수 AST가 동일하다. 결과 projection과
serializer 본문, metadata dict, CLI main도 동일하다. Worker 제어 흐름은 downloader 주입과
metadata 함수 추출을 정규화한 AST로 비교했다. 비교 helper와 결과는 로컬 evidence에 보관한다.

ID·시각·semantic pin을 고정한 전후 비교:

| 항목 | 바이트 | SHA-256 |
|---|---:|---|
| Input identity | 1,363 | `fd11891b4aa4086c5b520cf947809ea9d165372aafe6ee99589a3ec45bc69851` |
| Worker terminal payload | 7,045 | `4d3a2369c28ebcfdb57c90d2742bb295fb67e3956c5bad3729d578b7a916d7f7` |
| 요청 JSON schema | 9,624 | `0ada0df1207fc8c38f0b90d239aeb9f439feaa7ff65648653eb0d8e5a35c2048` |
| Context vector digest 결과 | 3,657 | `5f66b1888148270bf6d037f69cc8b35916a3c688b7e18d2d79d4771a06883c55` |

전체 bytes와 input/config hash·fingerprint·worker result hash가 일치했다. Context fixed vector의
resultHash는 `29fc9f6665cfe683c8d1a17be8292ab756a3057306738a06e3f6c752ecc4ff5d`다.
새 배포 artifact의 implementationDigest 등록이나 서로 다른 pin 간 hash 동일성을 검증한 것은 아니다.

## 4. 검증 결과와 한계

- 직접 영향 회귀: 81 passed. 새 worker 경계 테스트 19개, 추가 금지 import 사례 7개를 포함한다.
- 전체 로컬 suite: 615 passed, 124 skipped. DB URL 없는 37개와 폐기된 PoC 87개는 skip이다.
- 실제 PostgreSQL Analytics suite: 7 passed, skip 없음. 새 전용 schema에 0001~0017을 적용했다.
- Formatter·Ruff·Pyright strict, 전후 `verify-contract-sync.ps1 -IncludeWeb` 통과.

실 DB 검증은 concurrent create·claim/reclaim·old claim fencing·retry, 공유 capacity·grant·취소와
실제 dependency provider를 사용하는 ASGI HTTP→worker→descriptor→payload→ACK→정리를 포함한다.
ACK는 PERSISTED, DELETED_GENERATION, STALE_ATTEMPT 세 경우를 검증한다. Artifact 다운로드만
비민감 fixed input callable로 대체한다. 실제 Core 서버를 통한 Analytics dispatch·Core persistence
E2E는 harness가 없어 NOT_RUN이며 Integration/AR-10의 후속 gate다.

최초 직접 영향 실행에서 Windows WinError 10014가 발생했고 뒤이어 Python AST 런타임 오류가
발생했다. 실패 로그를 보존했으며 코드 우회 없이 새 프로세스 재실행에서 81개가 통과했다.
전체 suite와 실제 DB 7개도 별도로 통과했다. 환경 실패 실행을 PASS로 계산하지 않았다.
계약 동기화의 직접 호출은 기존 jsonschema 경고가 PowerShell 오류로 승격되어 중단됐다.
별도 PowerShell 프로세스 재실행은 종료 코드 0으로 통과했으며 두 로그를 모두 보존했다.

Analytics 계산은 기존처럼 worker 프로세스에서 동기 실행된다. 긴 Engine 계산은 중간에
강제 중단할 수 없으며 stop 확인 시점까지 계속된다. Heartbeat DB 호출이 막히면 join 1초 뒤에도
thread가 남을 수 있다. 이번 구조 변경은 새 timeout·process isolation·종료 정책을 적용하지 않는다.
AR-08 정책 결정, AR-09 구현, AR-10 운영 검증에서 다룬다.

로컬 실행 로그·JUnit·전후 payload·AST 비교·source fingerprint·checksum은
[AR-05A manifest](../../../integration/.integration-data/architecture-refactoring/ar05a/20261029T101841Z/manifest.json)에
보관한다. 이 위치는 Integration의 비커밋 산출물이며 credential을 포함하지 않는다.

## 5. Projection scalar 재사용 후속 개선

2026-10-30에는 metric scalar 변환·정렬을 호출마다 한 번 수행하고 observation용 unit index를
재사용했다. 반환 row의 독립성과 기존 순서·전체 hash를 보존한다. 전후 36개 합성 표본,
전체 suite·실제 PostgreSQL·32 episode ASGI/worker/ACK smoke와 미검증 Core E2E 범위는
[projection 개선 검증](ANALYTICS_PROJECTION_SCALARS.md)에 기록했다. 실행 제한·격리 정책은 별도다.
