# AR-11D — Package 진입점 경계

2026-10-31. `src/refinvest/__init__.py`가 모든 refinvest import에서 FastAPI app을 조립하던
부수 효과를 제거한다. HTTP 실행은 명시적 진입점에서 시작한다. 계산·시간·실행 의미론은 바꾸지 않는다.

## 1. 변경과 호환 범위

Root `main()`은 호출 시 기존 `app.main.main()`을 import해 위임한다. 추가 설정·exception 변환이나
별도 app factory는 만들지 않는다. 기존 main의 uvicorn target·host·port, startup 실패 전파와
정식 ASGI singleton을 유지한다. `pyproject.toml`의 11개 console target, systemd와 integration의
ASGI target도 변경하지 않는다. Root의 main은 같은 호출 동작을 제공하는 wrapper이며 기존 함수
객체의 identity를 보존하는 재수출은 아니다.

| 사용 방식 | 변경 후 |
|---|---|
| `import refinvest` | HTTP app을 import/조립하지 않음 |
| `refinvest:main`, `uv run refinvest` | 기존 main을 호출 시 import하고 기존 uvicorn 실행 인자로 위임 |
| `refinvest.app.main:main` | 기존 구현 그대로 |
| `refinvest.app.main:app`, `from refinvest.app.main import app` | 기존 FastAPI 객체·공식 ASGI 경로 유지 |
| `refinvest.app` | 정상 하위 package namespace |
| `refinvest:app`, root FastAPI `app` 재수출·star import의 app | 제거; 공식 ASGI 경로로 변경 필요 |

**Root의 Python app 별칭 제거는 호환성 변경이다.** 저장소·배포·integration의 사용처를 확인했고
운영 템플릿은 이미 공식 ASGI 경로를 쓴다. 별도 호출자가 이전 root 별칭을 쓰면 위의 공식 경로로
바꿔야 한다. HTTP OpenAPI나 Core 호출 계약의 변경은 아니다.

이전 root의 FastAPI `app`과 실제 `refinvest.app` package 이름이 충돌하므로 단순 module
`__getattr__`를 사용하면 선행 import에 따라 다른 객체를 반환할 수 있다. ASGI proxy나 module
class 교체로 충돌을 숨기지 않고 root의 미사용 FastAPI 재수출을 제거했다. 새 `__main__.py`나
`python -m refinvest` 실행 경로도 선제 추가하지 않는다.

## 2. 커밋과 검증 경계

1. 기존 코드에 CLI target 두 개, 설치된 console script, 정식 ASGI singleton과 startup 실패
   전파의 5개 사례를 추가하고 관련 suite 229개를 통과시킨 뒤 `db2d0cf`로 먼저 커밋했다.
2. Root 변경, cold import·실제 spawn 검사와 정적 import 규칙·문서를 함께 묶는다.

추가 24개 사례는 root·계산·세 worker의 cold import 8개, 선언된 console target 로딩 11개,
import 순서 3개, namespace/export 1개와 실제 spawn child 1개다. 별도 Python interpreter에
import blocker를 설치해 HTTP가 먼저 로딩된 pytest process의 cache로 오탐 통과하지 않게 한다.
Cold child는 기존 Windows 네트워크 bootstrap과 REFINVEST/AWS 설정도 제거한다.
정적 검사에는 root의 app 재수출·config·FastAPI·uvicorn 직접 import 금지 사례 4개를 추가한다.

순수 모듈·worker 검사는 FastAPI/Starlette/uvicorn/API import 자체를 차단한다. 모든 CLI 로딩은
`app.main`과 API route 조립을 차단하지만 benchmark의 기존 API DTO 의존까지 제거한 것은 아니다.
실제 spawn probe와 기존 worker process/factory·timeout·종료·lease 관련 회귀를 함께 검증한다.
Deadline을 완화하거나 API 조립 제거를 이전 AR-11C 간헐 실패의 확정 원인으로 해석하지 않는다.

## 3. 로컬 설치 metadata

검증 시작 시 Windows editable 설치 metadata에는 이미 pyproject에 있는 Analytics worker·cleanup
console 두 개가 누락돼 있었다. 최초 관련 suite는 이 두 target의 metadata 조회에서
2 failed / 255 passed였다. 환경 문제를 통과로 해석하거나 검사를 완화하지 않았다.
`uv pip install --offline --no-deps --reinstall --editable . --python .venv/Scripts/python.exe`로
프로젝트 metadata를 갱신했다. 외부 의존성·잠금파일·pyproject target은 변경하지 않았으며
갱신 후 11개 설치된 console entrypoint를 각각 cold interpreter에서 로딩해 확인했다.

## 4. 검증 결과

- Runtime OpenAPI 전후 44,951 bytes와 SHA-256
  `fa9da4fa83f1bc4b222165346d6dea807515a60355a15293d97cbd62429825af` 동일.
- Ruff·formatter 266 files, 기존 src strict Pyright 0 errors/0 warnings.
  전후 `verify-contract-sync.ps1 -IncludeWeb` 통과. `app/main.py` 바이트는 변경하지 않는다.
- 변경 후 Windows 관련 suite **257 passed**, 실제 spawn·worker 회귀를 확대한 native Linux
  관련 suite **298 passed**. Windows 전체 **837 passed / 131 skipped**, 기존 AnyIO assert rewrite 경고 1개.
- Core `9106c26bb1080ed8fd5d66aec365361de358e153`의 현재 source를 Gradle로 기동해
  JWT/CSRF → Core dispatch → Compute worker → Core terminal persistence smoke를 통과했다.
  Same-market·cross-market·drawdown·replay는 COMPLETED, 의도한 달력 오류는
  CALENDAR_RESOLUTION_FAILED를 유지한 FAILED다. Run ID는 `20261031T054216Z`이며 로컬
  integration `.integration-data/architecture-refactoring/ar11-package/20261031T054216Z/summary.json`에
  PASS 증적을 보존했다. 분리 DB `refinvest_integration`, 전용 Compute schema·immutable fixture를
  사용했다. 종료 후 해당 registry는 비어 있으며 Core source 변경 없이 실행 전후 revision과
  clean 상태를 확인했다.
- Core 첫 기동은 Gradle daemon의 Windows `Bad address: listen`으로 실패했다. 정본 harness가
  해당 실패 로그를 보존하고 소유 프로세스만 재시작했으며 두 번째 시도에서 READY가 됐다.
  `core-api-startup.json`의 첫 실패를 지우거나 최초 기동부터 성공한 것으로 표시하지 않는다.

Windows 전체 검증은 기존 외부 IPv6 bootstrap을 사용하되 신규 cold interpreter 검사는 이를
제거한다. Native Linux 검증은 bootstrap 없이 실행하며 skip은 성공으로 계산하지 않는다.
검증 시점의 Compute source 지문과 최종 source를 비교한다. PR_BODY.md·credential·fixture·로컬
증적은 커밋하지 않는다.

## 5. 남은 범위

Benchmark 조율은 후속 [AR-11E](BENCHMARK_CLI_BOUNDARY.md)에서 feature 경계로 옮겼다.
Ingestion·cleanup CLI의 업무 조율, 나머지 re-export의 필요성 검토와 최종
architecture/runbook 정리는 후속 개별 PR이다. AR-11 전체를 완료 표시하지 않는다.
ADR-071은 계속 DRAFT이며 production Analytics 격리·실행 한도·Core Analytics runtime E2E의
gate는 별도로 남아 있다. 이번 변경을 성능 수치 개선이나 새로운 실행 제한 정책으로 해석하지 않는다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
