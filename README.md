# refinvest-compute

Trading Review Analytics Job deployment and verification: [docs/TRADING_ANALYTICS_RUNTIME.md](docs/TRADING_ANALYTICS_RUNTIME.md).

내부 Backtest Engine 및 Market Data Ingestion 서비스다. 계산 계약과 작업 규칙은
[`AGENTS.md`](AGENTS.md)에서 시작한다.

아키텍처 개선의 작업 순서·PR 경계·검증 기준은
[`docs/ARCHITECTURE_REFACTORING_PLAN.md`](docs/ARCHITECTURE_REFACTORING_PLAN.md)에 정리한다.
현재 프로세스·의존 방향은 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
AR-00~07·AR-11의 구조 개선 완료 범위와 남은 Analytics 운영 gate는
[`docs/ARCHITECTURE_FINAL_AUDIT.md`](docs/ARCHITECTURE_FINAL_AUDIT.md)를 참고한다.
AR-00 실행 결과와 남은 검증 항목은
[`docs/ARCHITECTURE_BASELINE_AR00.md`](docs/ARCHITECTURE_BASELINE_AR00.md)를 참고한다.

고정 integration fixture로 결정성과 단계별 실행 비용을 측정하려면
[`docs/BENCHMARKING.md`](docs/BENCHMARKING.md)를 참고한다.

개발·검증 환경의 Prometheus 계측은 [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md)를 참고한다.

HTTP API는 `uv run refinvest` 또는 `uvicorn refinvest.app.main:app --host 0.0.0.0 --port 8000`으로
실행한다. Python에서 FastAPI 객체가 필요하면 `from refinvest.app.main import app`을 사용한다.
`refinvest:app`과 root의 FastAPI `app` 재수출은 제거했다. `refinvest.app`은 이제 하위 package
namespace다. 계산·worker import가 HTTP app을 조립하지 않도록 한 변경과 호환 범위는
[`docs/PACKAGE_ENTRYPOINT_BOUNDARY.md`](docs/PACKAGE_ENTRYPOINT_BOUNDARY.md)에 정리한다.

```powershell
uv run refinvest-benchmark --fixture-manifest <path> --snapshot-path <path> `
  --scenario <path> --warmups 3 --iterations 10 --cache-mode warm --output <path>
```
