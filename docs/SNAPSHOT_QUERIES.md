# Snapshot 조회 경계 — AR-07

상태: 변경 전 기준 확보. 기준 커밋은 `379eba5`이며 실제 요청 경로의 결과·오류와 비용을
고정한 뒤 application port·dependency provider 연결과 요청 안의 로딩 재사용을 진행한다.
검증 run: `20261030T031947Z` (UTC).

## 1. 커밋과 PR 경계

하나의 `main` 기준 PR에서 다음 두 커밋을 검토한다.

1. 고정 합성 Snapshot과 HTTP characterization, 변경 전 로딩 횟수·비용·오류 기준.
2. 조회 port·loader 조립 경계 연결, 요청 내 단일 로딩, legacy import 예외 제거와 관련 검증.

각 커밋은 관련 테스트와 문서를 포함한다. 변경 전 기준 커밋에서도 기존 동작을 유지한다.
후속 PR을 중간 브랜치로 머지하는 연쇄 구조는 사용하지 않는다.

## 2. 변경 전 기준

외부 vendor나 AWS 계정 없이 다섯 자산의 2023-01-01~2025-12-31 합성 daily 데이터를 만든다.
미국 주식은 평일, BTCUSDT는 매일의 독립 세션을 가진다. Manifest·calendar·daily bars를
실제 loader로 읽고 SHA-256 검증과 parsing을 수행한다. 요청마다 같은 immutable Snapshot을
선택한다. Filesystem은 실제 로컬 파일, S3는 메모리 object client를 사용한다.

세 번의 HTTP 요청에서 중앙값을 구했다. 네트워크 S3 지연·부하 시험·운영 latency의 추정치가
아니며 loader 호출 수와 artifact 읽기량을 핵심 비교 기준으로 사용한다.

| Backend / 경로 | loader 호출 | artifact 읽기 | 읽은 bytes | 중앙값 ms |
|---|---:|---:|---:|---:|
| Filesystem `/assets` | 5 | 15 | 4,622,095 | 455.19 |
| Filesystem `/assets/QQQ/availability` | 1 | 3 | 924,419 | 100.10 |
| Filesystem `/series` | 1 | 3 | 924,419 | 80.33 |
| 메모리 S3 `/assets` | 5 | 15 | 4,622,095 | 402.31 |
| 메모리 S3 `/assets/QQQ/availability` | 1 | 3 | 924,419 | 69.81 |
| 메모리 S3 `/series` | 1 | 3 | 924,419 | 76.94 |

`/assets`와 availability는 latest를 한 번, 명시적 `/series`는 get을 한 번 호출한다.
기존 `/assets`의 문제는 Snapshot 선택 횟수가 아니라 동일 Snapshot의 전체 로딩 반복이다.
Calendar만 별도로 읽고 daily bars의 기존 무결성 검증을 생략하는 최적화는 하지 않는다.

## 3. 보존할 응답과 오류

- 다섯 자산의 순서·상장일·availability와 Snapshot ID·createdAt을 유지한다.
- Snapshot 부재 시 `/assets`는 정적 목록과 생략된 availability, 개별 availability는 503이다.
- 명시적 series Snapshot 미존재는 404, latest 부재는 503이다.
- `/series`의 PRICE·RETURN·NORMALIZED 계산, 자산별 실제 세션과 기존 point·기간 제한을 유지한다.
- Expected close 결측과 손상 artifact는 series의 기존 503 응답을 유지한다.
- Assets 두 경로의 손상 artifact는 현재 500이다. 이 구조 변경에서 새로운 오류 정책을 선택하지 않는다.
- 각 요청은 artifact를 새로 검증한다. 이전 요청의 성공을 이유로 변조된 파일이나 S3 object를
  재사용하지 않는다. 전역 cache나 latest의 장기 보관은 도입하지 않는다.
- Ingestion의 artifact → manifest → metadata 발행과 Backtest 실행 loader는 변경하지 않는다.

정본은 Context ADR-010/046/049/050과 현재 OpenAPI다. 로컬 초기 ADR-045/046에 남은
여섯 자산 설명을 현재 다섯 자산 계약보다 우선하지 않는다. Series는 보류된 내부 capability이며
MVP Web에 새로 노출하지 않는다.

## 4. 검증

변경 전 HTTP·기존 Asset/Series 테스트 23개와 contract-sync `-IncludeWeb`을 통과했다.
추가 HTTP 검사는 두 backend에서 Snapshot 선택 횟수, 정상 응답, 인증·입력 오류의 읽기 금지,
교차 달력 세션, 요청 사이 artifact 변조를 확인한다. 재현 명령은 다음과 같다.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_snapshot_query_routes.py tests/test_asset_routes.py tests/test_series.py
```

테스트와 비용 측정은 기존 opt-in IPv6 loopback 검증 환경을 사용했다. AnyIO 선행 import의
assertion-rewrite warning 한 건을 기록했으며 production 설정에는 포함하지 않는다.
Raw log·응답 bytes·측정 JSON·fixture는 로컬 run 디렉터리에 보존하고 커밋하지 않는다.
구현 후 동일 HTTP bytes·읽기 횟수·오류·전체 회귀와 Core smoke를 추가 검증한다.

Determinism impact: none. Point-in-Time impact: none.
