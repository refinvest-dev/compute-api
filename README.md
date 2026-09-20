# refinvest-compute

내부 Backtest Engine 및 Market Data Ingestion 서비스다. 계산 계약과 작업 규칙은
[`AGENTS.md`](AGENTS.md)에서 시작한다.

고정 integration fixture로 결정성과 단계별 실행 비용을 측정하려면
[`docs/BENCHMARKING.md`](docs/BENCHMARKING.md)를 참고한다.

```powershell
uv run refinvest-benchmark --fixture-manifest <path> --snapshot-path <path> `
  --scenario <path> --warmups 3 --iterations 10 --cache-mode warm --output <path>
```
