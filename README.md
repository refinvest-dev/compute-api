# refinvest-compute

Trading Review Analytics Job deployment and verification: [docs/TRADING_ANALYTICS_RUNTIME.md](docs/TRADING_ANALYTICS_RUNTIME.md).

내부 Backtest Engine 및 Market Data Ingestion 서비스다. 계산 계약과 작업 규칙은
[`AGENTS.md`](AGENTS.md)에서 시작한다.

아키텍처 개선의 작업 순서·PR 경계·검증 기준은
[`docs/ARCHITECTURE_REFACTORING_PLAN.md`](docs/ARCHITECTURE_REFACTORING_PLAN.md)에 정리한다.
AR-00 실행 결과와 남은 검증 항목은
[`docs/ARCHITECTURE_BASELINE_AR00.md`](docs/ARCHITECTURE_BASELINE_AR00.md)를 참고한다.

고정 integration fixture로 결정성과 단계별 실행 비용을 측정하려면
[`docs/BENCHMARKING.md`](docs/BENCHMARKING.md)를 참고한다.

개발·검증 환경의 Prometheus 계측은 [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md)를 참고한다.

```powershell
uv run refinvest-benchmark --fixture-manifest <path> --snapshot-path <path> `
  --scenario <path> --warmups 3 --iterations 10 --cache-mode warm --output <path>
```
