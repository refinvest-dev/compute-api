# Snapshot 조회 경계 — AR-07

상태: AR-07 완료. 구현·전후 비교·전체 회귀·고정 Core smoke를 통과했다.
기준 커밋은 `379eba5`이며 실제 요청 경로의 결과·오류와 비용을 고정한 뒤
application port·dependency provider 연결과 요청 안의 로딩 재사용을 구현했다.
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

## 4. 구현과 변경 후 비교

HTTP는 기존 `DatasetSnapshotRepository`와 `SnapshotExecutionDataLoader` port를 사용한다.
`api/dependencies/assets.py`가 설정·Postgres repository·loader를 조립하며 loader의 S3 client는
실제 load까지 지연된다. 별도의 조회 framework나 cache service는 추가하지 않았다.

`application/assets.py`의 `availabilities_for_snapshot`은 한 번의 전체 검증 후 함수 지역 변수의
Calendar를 재사용한다. 기존 단일 자산 helper와 조회 순서, 반환값도 유지한다. Series의 계산
함수·JSON 수치 정밀도는 변경하지 않는다. 여섯 legacy 예외는 모두 제거해 registry가 비었다.

`SnapshotArtifactError`는 loader port가 있는 application 모듈로 옮겼다. Filesystem/S3가 같은
오류 class를 사용하고 API는 application 오류를 처리한다. 기존 storage 모듈도 해당 class를
직접 import해 사용하므로 기존 consumer의 오류 catch와 메시지는 유지된다. 새 wrapper나
자동 fallback은 만들지 않았다. 오류 모듈 소유권 외 artifact parsing·해시 검증은 그대로다.

| Backend / `/assets` | loader 전 → 후 | 읽기 전 → 후 | bytes 전 → 후 | 중앙값 ms 전 → 후 |
|---|---|---|---|---|
| Filesystem | 5 → 1 | 15 → 3 | 4,622,095 → 924,419 | 455.19 → 77.27 |
| 메모리 S3 | 5 → 1 | 15 → 3 | 4,622,095 → 924,419 | 402.31 → 72.14 |

두 backend의 catalog·availability·RETURN series, 총 여섯 HTTP 결과의 전체 bytes가 같다.
단일 availability/series는 전후 모두 loader 1회·읽기 3회다. 세 번의 로컬 시간 표본은 변동성이
있고 series의 Filesystem 중앙값은 80.33 → 131.05ms였다. 이 표본으로 모든 경로의 latency
개선을 주장하지 않으며, 이번 변경의 확정된 효과는 catalog의 중복 로딩·읽기량 감소다.

## 5. 검증

변경 전 HTTP·기존 Asset/Series 테스트 23개와 contract-sync `-IncludeWeb`을 통과했다.
추가 HTTP 검사는 두 backend에서 Snapshot 선택 횟수, 정상 응답, 인증·입력 오류의 읽기 금지,
교차 달력 세션, 요청 사이 artifact 변조를 확인한다. 재현 명령은 다음과 같다.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_snapshot_query_routes.py tests/test_asset_routes.py tests/test_series.py
```

테스트와 비용 측정은 기존 opt-in IPv6 loopback 검증 환경을 사용했다. AnyIO 선행 import의
assertion-rewrite warning 한 건을 기록했으며 production 설정에는 포함하지 않는다.
Raw log·응답 bytes·측정 JSON·fixture는 로컬 run 디렉터리에 보존하고 커밋하지 않는다.
구현 후 직접 영향 회귀 112개와 전체 회귀 681개를 통과했다. 전체 skip 131개는 DB URL 없는
44개와 폐기된 PoC 87개다. Formatter·Ruff lint·Pyright strict도 통과했다. Contract-sync는
전후 모두 `-IncludeWeb`으로 통과했다. 생성되는 runtime OpenAPI도 전후 전체가 동일하다.

요청 사이에 새 Snapshot을 선택하는 경우와 artifact 변조, coverage 없는 asset, 인증·입력
오류에서 load가 없는 경우, provider의 요청별 객체 생성도 검증했다. 기본 Windows IPv4
문제가 해소됐다는 의미는 아니며 검증용 bootstrap은 production에 넣지 않는다.

Core smoke는 고정 Core `206473c`와 별도 integration Compute schema/root를 사용한다.
동일시장·교차시장·수수료·결정론 replay는 COMPLETED, 예상 데이터 오류는 FAILED로 처리되고
Core에 결과가 저장되는 전체 흐름을 통과했다. 종료 후 소유한 세 프로세스 registry는 비었다.
Canonical harness의 Core public schema·기본 bootRun 실행 identity 규칙을 따른다. 최초
시도들의 schema·JAR 실행 방식 불일치와 Windows listen 재시도 로그도 보존한다. 이를 제품
코드의 API 성공·실패로 해석하지 않는다. 현재 Core checkout `12aba18`의 E2E를 대신하지 않는다.

Raw manifest와 전후 응답·측정·JUnit·Core smoke는 integration의 무시된
`.integration-data/architecture-refactoring/ar07/20261030T031947Z/`에 보존한다.

Determinism impact: none. Point-in-Time impact: none.
