# AR-00 Architecture Baseline

실행일: 2026-10-28. Run ID: `20261028T085406Z`.

상태: AR-00 완료. 계약·로컬 계산·PostgreSQL·Backtest/Normalization Core E2E의
기준 결과를 확보했고, 2026-10-28 포맷 보완에서 기존 formatter 실패 24개를 해소했다.
포맷 변경 전후 AST·주석 토큰 동일성과 formatter·lint·Pyright 통과를 확인했다.
아키텍처 리팩토링, 계산 로직·기대 hash 변경, 새 계산·운영 정책 적용은 수행하지 않았다.

## 1. 비교 대상

| 대상 | 실행 기준 commit | 실행 시작 시 작업 트리 |
|---|---|---|
| Compute | `0701c4ac07ac89281fa6f1b263bde294b0db6452` | README·계획 문서 및 기존 PR 보조 파일 변경; src/tests/migrations 변경 없음 |
| Context | `06fdd057100917444aad36ff997e483f0f4f588b` | 로컬 PR 보조 파일만 untracked |
| Core | `206473c6e7c5214d23c731935e9ead703507a2b6` | clean |
| Integration | `a840dcf6d7a4b05c45cc7864a21dcda6512b1d9d` | 로컬 문서·Python cache untracked |
| Web | `2ef516694dd45f79eba6ecfbb99f0a424f9aa4dc` | AGENTS.md 변경 |

실행 도중 원래 Core 작업 트리에 별도 소스 변경이 관측되었다. 해당 파일을 수정·복원하지
않고 최초 Core commit의 detached worktree를 만들어 Backtest를 다시 실행했다.
최종 기준 E2E의 Core는 `206473c` clean worktree다. 변경 중 작업 트리를 사용한 중간
Backtest PASS는 보조 관측으로만 보관한다. Normalization은 앞서 빌드한 고정 JAR를
사용했으며 그 SHA-256과 크기(72,009,537 bytes)를 manifest에 기록했다.

Compute의 `openapi/CONTEXT_REVISION`은 `a9d169607672e51b18310a40a9671693e21bf305`이고
OpenAPI 버전은 `0.6.0`이다. Context checkout HEAD와 revision marker를 구분했으며,
실제 계약·fixture·Core 문서 동기화 여부는 verifier로 확인했다.

Compute의 tracked src/tests/migrations/openapi 및 pyproject.toml·uv.lock·.python-version을
파일명 순으로 결합한 SHA-256은 다음과 같다. 알고리즘과 파일 범위는 실행 manifest에 대응한다.

```text
df714c54a0e2858a74e7501855f5a763b8488eb0c99c34130986bb143fa6458d
```

| 환경 | 기준 |
|---|---|
| OS | Windows 10 build 26200, AMD64 |
| Python | 저장소 .venv, CPython 3.11.5 |
| FastAPI / Pydantic | 0.141.1 / 2.13.4 |
| psycopg / PostgreSQL server | 3.3.4 / 16.4 |
| pytest / Ruff / Pyright | 9.1.1 / 0.16.4 / 1.1.411 |
| cryptography | 50.0.2 |
| uv.lock SHA-256 | `8b5735f18b372f4a64cc8c6c8ca1d87d8ba85e3af28ce2bade098e20a3fbc75c` |

## 2. 결과와 재현 명령

| 검사 | 결과 | 관측 |
|---|---|---|
| Contract sync `-IncludeWeb` | PASS | Core/Compute/Web OpenAPI, 문서, Normalization V1/V2, Analytics V1 vector 검증 |
| Ruff lint | PASS | All checks passed |
| Ruff formatter | PASS (포맷 보완) | 최초 24개 실패를 정리한 뒤 221 files already formatted |
| Pyright strict | PASS | 0 errors, 0 warnings |
| 전체 pytest, DB URL 미설정 | PASS + SKIPPED | 463 passed / 117 skipped, 재실행 10.51초 |
| PostgreSQL 통합 테스트 | PASS | 30 passed / 0 skipped, 21.50초 |
| Backtest Core E2E | PASS | 고정 Core checkout에서 동일시장·교차시장·drawdown·결정론 replay·예상 실패의 Core 저장 |
| Normalization publication·version pin E2E | PASS | V1 publication·ACK·result integrity, ORCH-024~028 |
| Analytics Core E2E | NOT_RUN | 기존 fixture·모의 worker/API·실 DB 테스트와 별도인 공백 |
| V2 Core publication E2E | NOT_RUN | 현재 harness OUT_OF_SCOPE; 구현 지원 여부와 구분 |
| 성능·load/stress·전체 crash 조합 | NOT_RUN | AR-07 또는 해당 운영 변경 단계에 배정 |

로컬 검사는 다음 명령으로 실행했다. Raw log와 JUnit은 로컬 증적 디렉터리에 둔다.

```powershell
.venv/Scripts/ruff.exe check src tests
.venv/Scripts/ruff.exe format --check src tests
.venv/Scripts/pyright.exe --pythonpath .venv/Scripts/python.exe
.venv/Scripts/python.exe -m pytest -q -rs --tb=short --junitxml=<local-evidence>/pytest-local-retry.xml
powershell -NoProfile -ExecutionPolicy Bypass -File ../../integration/scripts/verify-contract-sync.ps1 -IncludeWeb
```

DB 테스트는 Integration의 환경 로딩·migration 절차로 준비한 아래 schema를 사용했다.
DSN은 local 환경 파일에서 메모리로 구성하고 process-local `REFINVEST_TEST_DATABASE_URL`에
설정했다. Credential을 명령 인자·문서·manifest에 기록하지 않았다.

```powershell
.venv/Scripts/python.exe -m pytest tests/integration -q -rs --tb=short --junitxml=<local-evidence>/pytest-postgres.xml
```

| DB 범위 | 실행 격리 |
|---|---|
| 대상 | loopback `refinvest_integration`, port 5432 |
| Compute 테스트 schema | `ar00_20261028t085406z` |
| Migration | 0001~0017, 새 schema에 17개 적용 |
| Backtest 통합 테스트 | 6 passed |
| Normalization 통합 테스트 | 20 passed |
| Analytics 통합 테스트 | 4 passed |

Backtest E2E는 별도 Compute schema `ar00_bp20261028t085406z`와 해당 격리 DB의 Core
`public` schema를 사용했다. Normalization은 `tnorm_c99574fac018`과
`tnorm_corec99574fac018`을 사용했다. 이번 실행에서 생성한 프로세스는 정리했고,
다섯 registry의 활성 role이 모두 제거된 것을 확인했다. 조사용 schema와 fixture는 보존했다.

과거 설명에서 skip 117개를 모두 DB 테스트로 분류한 것은 부정확했다.
실제 분류는 DB 미설정 30개와 `test_binance_reconstruction_golden.py`의 의도적으로 폐기된
test-only PoC 87개다. 후자는 production engine regression으로 대체되었다는 skip 사유를
유지했다. 이번 실 DB 실행으로 30개를 추가 검증했으며, 서로 다른 실행을 합친 범위는
493 passed와 의도적 skip 87개다. 전체 suite 한 번에서 493개가 통과했다고 표기하지 않는다.

## 3. 보존 항목과 검증 coverage

| 경계 | 이번 실행에서 재사용한 테스트 |
|---|---|
| Temporal·결정론 | test_cross_calendar_metric_anchor, test_signal_lag_resolution, test_temporal_sessions, test_daily_bar_backtest_executor |
| 체결·성과·표본 | test_position_admission, test_trade_realization, test_equity_curve_performance, test_backtest_result_assembly |
| Normalization 계산 | test_trading_review_engine, test_trading_review_determinism, test_trading_review_invariants |
| Identity·V1/V2·무결성 | test_trading_normalization_input_identity, test_trading_review_result_integrity, test_trading_result_delivery |
| Analytics 세 Metric·projection | test_trading_review_analytics, test_trading_analytics_runtime |
| Worker·IPC·오류 | test_backtest_process_executor, test_trading_review_worker_runtime, test_trading_analytics_runtime |
| DB concurrency·발행·ACK | 세 tests/integration/test_postgres_*_jobs.py |

Normalization의 취소·완료 동시 호출은 별도 로컬 characterization probe로 보완했다.
동시 시작, 취소 선행, 발행 선행을 각각 3회 실행해 총 9회 모두 최종 상태와 payload 유무가
일치했다(CANCELLED 6회, COMPLETED 3회). 완료 후 취소의 best-effort 의미도 보존했다.
`publication-race.py`와 결과 JSON을 로컬 증적으로 남기며 30개 pytest 수에는 합산하지 않는다.
Analytics의 모든 취소·발행 interleaving을 이 결과로 대신하지 않으며 AR-06/09에서 보완한다.

기존 테스트로 현재 리팩토링 대상의 고위험 기준을 확인했다. 저장소의 테스트 코드와
golden 기대값은 변경하지 않았다. 표는 coverage 연결 관계이며, 모든 crash·순서 조합을
망라했다는 뜻은 아니다. Runtime 변경 단계에서 해당 경쟁·종료 시나리오를 다시 검증한다.

## 4. Fixture와 지원 버전의 의미

Normalization의 실행 registry는 현재 `SUPPORTED_VERSION_SET`을 사용한다.
공개 결과는 V1/V1과 V2/V2의 명시적 pin, Analytics는 V1 세 Metric과 disabled Finding을
fixed vector·API·DB 수준에서 확인했다. Analytics fixture의 implementationDigest는
합성 값이다. 이 검증을 production image digest의 배포 가용성 확인으로 해석하지 않는다.

다음 fixture의 파일 SHA-256과 Normalization VersionSet 전체는 실행 manifest에 기록했다.

- normalization_identity/v1/input-identity-v1.json
- normalization_identity/v1/result-integrity-v1.json
- normalization_identity/v2/result-integrity-v2.json
- analytics_protocol/v1/input-identity-v1.json
- analytics_protocol/v1/result-integrity-v1.json

Backtest E2E는 합성 immutable Snapshot `integration-e2e-v1`을 사용한다.
해당 manifest 파일 SHA-256은
`b5ffd432c9be12c9e9d1a8e5f4ed1659fd9135d52590a5d62edaf91331046487`이며,
Calendar·daily bars의 파일 hash도 manifest에 기록했다.
다른 fixture 실행에서 새 Job·Run·Artifact ID가 생성되므로 서로 다른 E2E run의 전체
resultHash 동일성을 요구하지 않는다. 각 실행 내부에서 정본 규칙으로 identity·hash를 검증한다.

## 5. 재시도와 기존 실패

| 항목 | 관측·처리 |
|---|---|
| 최초 local pytest | test_submission_rejects_idempotency_key_reused_for_another_request에서 Windows socketpair WinError 10014. 이후 pytest traceback 출력 중 AST recursion SystemError로 중단 |
| Local pytest 재실행 | 코드 변경 없이 subprocess 파일 capture와 short traceback으로 전체 종료, 463 passed |
| 최초 contract capture | PowerShell 5.1 redirection이 jsonschema DeprecationWarning을 NativeCommandError로 승격 |
| Contract 재실행 | verifier 변경·warning 무시 없이 별도 PowerShell process의 stdout/stderr를 OS 수준에서 수집, exit 0 |
| 최초 migration 호출 | 별도 PowerShell -File의 기본 인자에서 PSScriptRoot가 비어 실패. 경로 인자를 명시하여 17개 적용 완료 |
| 최초 Backtest Core smoke | JAR 실행 identity와 기존 smoke가 기대한 bootRun identity 불일치. 사전 검사에서 거절하고 생성한 프로세스만 정리 |
| 두 번째 Backtest 시작 | Compute API의 Windows socketpair WinError 10014로 readiness 실패. 기존 bounded startup helper로 재실행 |
| Core 작업 트리 변경 | 실행 도중 별도 Core 소스 변경을 관측. 원본을 수정·복원하지 않고 최초 commit의 detached worktree로 기준 E2E 재실행 |

초기 실패 로그도 보존한다. 통과한 재실행이 앞선 환경 불안정성을 없던 일로 만들지는 않는다.
최초 baseline 실행에서는 formatter 차이를 수정하지 않고 기록했다.
대상 파일 목록은 로컬 `formatting-debt.json`에 보존했으며, 아래 포맷 보완에서
해당 24개 파일만 정리하고 검사를 다시 수행했다.

## 6. 증적과 후속 gate

로컬 archive 위치는 Integration의
`.integration-data/architecture-baseline/20261028T085406Z/`다. Manifest, 원본 로그, JUnit,
재현 driver 등 파일 25개를 보관했다. Core worktree와 빌드 디렉터리는 복사하지 않았다.
Credential을 포함하는 runtime.env와 생성 artifact는 로컬에서만
관리한다. 이 디렉터리와 compatibility evidence는 Compute에 커밋하지 않는다.

- [실행 manifest](../../../integration/.integration-data/architecture-baseline/20261028T085406Z/manifest.json)
- [고정 Core Backtest 결과](../../../integration/.integration-data/architecture-baseline/20261028T085406Z/backtest-pinned/backtest-summary.json)
- [Normalization E2E 증적](../../../integration/.integration-data/trading-normalization/c99574fac0184535bc62d61a31a04664/evidence.json)

재현 시 원래 Core 작업 트리의 현재 상태 대신 manifest의 commit을 checkout한다.
Archive의 `backtest-baseline.ps1`은 기존 Integration 스크립트를 조합하는 로컬 driver이며
`-IntegrationRoot`, `-ComputeRoot`, `-CoreRoot`, `-PinnedCore`를 받는다.
Normalization은 기존 `test-trading-normalization-result-version-pin.ps1`에 같은 기준
checkout과 전용 포트를 지정한다. 환경·기동 절차는 Integration 정본을 따른다.

| 남은 항목 | 담당·시점 |
|---|---|
| Windows socketpair·pytest reporting 불안정 | 실행 환경 이슈로 추적; 관련 runtime 변경 시 재현 여부 확인 |
| Analytics 실제 Core dispatch·publication·ACK E2E | Integration 후속 작업; AR-10 운영 완료 gate |
| V2 Core publication 지원 여부·E2E | 해당 Core/Integration 후속 범위에서 지원 여부 확인·검증 |
| Snapshot 로딩 횟수·조회 비용 | AR-07 변경 직전 |
| Temporal profile·메모리/강제 종료 stress | 해당 최적화·AR-09/10 범위 |

기존 formatter FAIL을 해소했으므로 AR-00 완료 gate를 충족했다.
후속 검증 공백은 각 담당 단계에서 계속 추적하며 이번 완료를 해당 경계의 검증 완료로
해석하지 않는다. 다음 작업은 AR-01 의존성 규칙 강화다.

## 7. 포맷 보완 및 완료 판정

보완 Run ID: `20261028T141631Z`. 원래 baseline manifest·실패 로그는 수정하지 않고
별도 결과를 추가했다. 적용 도구는 같은 Ruff `0.16.4`다.

| 확인 | 결과 |
|---|---|
| 포맷 대상 | 기존 formatting-debt 목록의 24개 파일만 변경 |
| AST 비교 | Python 파일 218개 모두 동일, type comment 포함 |
| 주석 토큰 비교 | 218개 파일 모두 내용·순서 동일 |
| Formatter | PASS, 221 files already formatted |
| Ruff lint | PASS |
| Pyright strict | PASS, 0 errors, 0 warnings |
| git diff --check | PASS |

AST는 `ast.parse(type_comments=True)` 후 위치 attribute를 제외해 비교했다.
AST field에 포함되는 `TypeIgnore.lineno`도 위치 정보로 정규화하고 tag는 보존했다.
주석은 별도로 tokenize하여 내용·순서를 비교했다. 변경된 파일 집합도 기존 24개 목록과
일치하는지 확인했다. API 계약·fixture·migration·의존성·설정은 변경하지 않았다.

원래 raw fingerprint는 §1과 기존 manifest에 보존했다. 포맷 보완 후 같은 파일 범위의
raw fingerprint는 다음과 같으며, AST가 동일한 포맷 차이로 구분한다.

```text
ece17a1cdf28b7921ac3f4c294d02afd2a416a306a28655d80c9386b889a796a
```

동작 변경이 없는 포맷 보완이므로 unit·DB·Core E2E는 이번 보완에서 재실행하지 않았다.
§2의 테스트·E2E 통과 기록은 최초 baseline 실행 결과이며, 새 실행 결과로 바꾸지 않았다.
Baseline 코드와 동일한 AST·주석을 확인한 결과를 연결해 완료 판정했다.

보완 증적은 기존 archive 아래 `formatting/20261028T141631Z/`에 둔다.
[보완 summary](../../../integration/.integration-data/architecture-baseline/20261028T085406Z/formatting/20261028T141631Z/summary.json)에
검사 결과와 전후 fingerprint를, state-before/after.json에 파일별 비교 hash를 기록했다.
보완 자료도 로컬 증적으로 관리하며 Compute에 커밋하지 않는다.

Determinism impact: none. Point-in-Time impact: none.
