# Compute Architecture 최종 점검

2026-11-01 KST. 기준은 PR #126이 병합된 Compute `0a48403`과 OpenAPI 0.6.0이다.
AR-00~07 및 AR-11의 기본 구조 개선을 완료한다. AR-08~10의 Analytics 실행 정책·운영 격리와
production 연결은 별도 미완료다. 이 문서는 Context의 제품 정책·ADR·OpenAPI를 변경하지 않는다.

## 1. 완료 범위와 유지한 경계

| 경계 | 현재 소유권·점검 결과 |
|---|---|
| 순수 계산 | engine과 trading_review, temporal·execution·정규화·세 Metric 의미 보존 |
| 계약 | contracts.normalization/analytics의 identity·canonicalization·integrity·전달 정책 |
| Application | use case·port·입력 검증·projection·worker 조율, concrete DB/HTTP/vendor에 의존하지 않음 |
| 바깥 구현 | 기능별 adapters의 artifact fetch·process, persistence의 DB/암호화/storage |
| App·HTTP | 설정·기술 구현·process/CLI 조립, api의 인증·오류·HTTP 계측과 provider |
| Snapshot·도구 | ingestion의 발행, benchmarking의 실행 조율, application의 reference-data 진단 |

전체 production Python 165개와 package initializer 36개를 점검했다. Root는 호출 시 import하는
`main`을 제공하고, Engine 8개·Trading Review 2개 package는 `__all__`로 순수 공개 facade를
제공한다. 이를 불필요한 compatibility re-export로 제거하지 않는다. 나머지 initializer는 비어
있거나 docstring만 있다. 이행 예외 registry는 빈 배열이다.

제거한 app security·identity·integrity·delivery·payload·normalization·Analytics input/result와
application Backtest process의 옛 모듈 10개는 존재하지 않는다. 이전 import/monkeypatch 검색의
남은 이름은 금지 import를 검사하는 synthetic test와 의도적으로 유지한 logger 이름이다.
실제 process target은 adapters.backtest에 있고, CLI·ASGI 경로는 진입점 검사로 확인한다.
문자열 검색이나 정적 import 검사만으로 모든 동적 import·간접 I/O가 없다고 증명하지는 않는다.

`app/`의 설정·HTTP·worker·storage·CLI 14개 파일을 대조했다. Cleanup 3개는 설정·DB 조립·
단일 bounded 호출·완료 로그를 유지한다. 새 공통 cleanup framework는 만들지 않는다.
Benchmark의 입력 DTO 재사용처럼 개발 도구에 남은 API schema 의존은 이미 알려진 별도 범위다.
모든 CLI가 FastAPI 자체에 독립적이라고 해석하지 않는다.

## 2. Fixture 도구의 명시적 유지 범위

`app/integration_fixture.py`는 비운영 고정 snapshot 생성·발행·재사용 검증을 담당하는 수동
composition 도구로 유지한다. Fixed ID·가격·session·provenance는 기존 테스트·local smoke 입력이다.
Production 계산·worker·vendor ingestion에서 import하지 않으며, 자동 fallback 데이터가 아니다.
이를 분리하려고 fixture 전용 generic port나 새로운 application 계층을 추가하지 않는다.

이 명령은 환경 변수로 받은 DB·storage를 사용하며 스스로 production DB를 식별·차단하지 않는다.
따라서 기존 integration 정본의 분리된 DB·schema·credential·고정 fixture 절차에서 실행한다.
Immutable 경로·기존 metadata 검사는 환경 격리를 대신하지 않는다. 향후 CLI 환경 보호를 강화할
경우 별도 운영 변경으로 검증한다. 이 유지 범위는 일반 app에 업무 조율을 추가하는 허용이 아니다.

## 3. 현재 구조와 문서 수정

[Architecture](ARCHITECTURE.md)에 실제 src package와 HTTP·worker 3개·단발 timer의 책임,
Core와 Compute의 데이터 소유권, 기능별 실행 차이를 반영했다. 개념상 추천 tree에만 있던
domain/benchmark/corporate_actions 디렉터리를 현재 구현처럼 표시하지 않는다.

API runbook의 6자산·1–6 symbol 설명을 실제 5자산·1–5로 맞추고, cleanup 로그를
`backtest_cleanup_completed.deleted_count`로 정정했다. Normalization runbook의 오래된 계약
버전과 Core E2E 후속 설명을 수정했다. PR stack 보고서는 #115 당시 기록임을 명시했다.
과거 단계별 테스트 수치·실행 증적은 현재 재검증 결과로 바꾸지 않는다.

## 4. 이번 검증과 이전 구현 검증의 구분

- 의존성·Trading Review 경계·진입점·systemd·fixture 검사 **149 passed**, 기존 AnyIO
  assert rewrite 경고 1개. Ruff·formatter 272 files·src strict Pyright도 통과했다.
- Git의 source·test·tool·migration·deploy·OpenAPI·lockfile 및 console 설정을 변경하지 않는다.
  비문서 tracked 파일의 전후 SHA-256과 PR_BODY.md 원본을 대조한다.
- Context `c4ef9e0`·병합된 Core main `c51d9cd`를 고정한 별도 clean checkout·현재 Compute·Web
  조합에서 `verify-contract-sync.ps1 -IncludeWeb` 통과. Core 문서·V1 vector·schema 검증을 포함한다.
  최초 검사는 Core 작업 폴더의 미커밋 compute-api.yaml 확장 70줄로 실패했다. 그 조합을 성공으로
  표시하거나 변경을 되돌리지 않고, main 조합 결과와 따로 기록했다.
- 로컬 Markdown 링크와 문서의 실제 모듈·진입점·unit·설정·정본 계약을 대조한다.

직전 구현 PR #126은 전체 909 passed / 131 skipped, Linux 관련 192 passed와 실제 진단 CLI·
Core main `c51d9cd`의 정상 4건/의도한 실패 1건을 통과했다(`20261031T133152Z`). 당시 전체 검사
2개 실패와 E2E 연결 실패 2회, 성공 실행의 Windows socket 기동 재시도는
[AR-11G](REFERENCE_DATA_DIAGNOSTICS_BOUNDARY.md)에 보존한다. 이번 main의 Git source/test tree는
그 구현과 동일하다. Checkout 시 새 진단 파일의 LF/CRLF 차이는 정규화해 확인했다.
이번 문서 작업에서 전체 suite·실 DB·Core E2E를 새로 실행했다고 주장하지 않는다.

## 5. 미완료 gate와 위험

| 항목 | 현재 한계 | 후속 조치 |
|---|---|---|
| Analytics 실행 예산 | heartbeat가 계산 정지를 끝내지 못함; 부모 내 실행, attempt 전체 deadline 없음 | DRAFT ADR-071 리뷰·운영 수치 승인 후 AR-09/10 구현·연결 |
| Analytics 메모리·IPC·DB | bytes cap은 RSS 상한이 아님; 최대 입력/output·동시 worker·부모 발행 예산 미완료 | 대표 workload·Linux/systemd hard limit·종료·발행 경합 검증 |
| Core Analytics E2E | 합성 vector·ASGI/DB·Normalization/Backtest E2E로 대체 불가 | 실제 Core dispatch→artifact→계산→장기 결과→ACK·삭제 경합 검증 |
| Windows 검증 안정성 | 기존 3초 process/descendant 테스트·Core 연결 실패 원인 미확정, 기동 socket 오류 관측 | 실패 증적 보존·환경/프로세스별 재현, production Linux 검증과 구분 |
| Fixture 환경 | CLI 자체의 production DB 차단 없음 | 분리된 local harness 절차 유지, 보호 강화는 별도 운영 변경 |
| 작업 폴더 계약 drift | Core의 미커밋 확장과 Context/main 계약이 다름; main 조합 검증만 통과 | 소유 작업에서 정본·호환성·snapshot 반영 순서 검증, 이번 문서 PR에 복사하지 않음 |
| 배포·migration | 원자적 발행 검증과 배포 runner의 transaction 적합성은 다름; ORCH-027 기존 위험 | 실제 autocommit migration 경로와 배포·rollback 절차 별도 검증 |
| 선택적 최적화 | deep immutability·전역 cache 상한·완료 시점 index 보장은 별도 | 실제 profile과 동일성 검증 후 개별 PR |

구조 개선 완료는 모든 운영 위험 해소나 Analytics production 격리 승인을 뜻하지 않는다.
다음 필수 작업은 [Analytics 실행 정책 검토](ANALYTICS_EXECUTION_POLICY_REVIEW.md)의 승인·
실측 gate이며 Backtest timeout 수치나 오류 code를 Analytics에 복사하지 않는다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
