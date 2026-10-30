# AR-08 Analytics 실행 정책 검토와 구현 gate

상태: **ADR 초안 제안 / 승인 대기 / production 미연결**. 2026-10-30.
정책 제안은 [Context ADR-071 초안](https://github.com/refinvest-dev/context/pull/43)과
[상세 문서](https://github.com/refinvest-dev/context/blob/fb741006cb51f36dbb76ebd20c009a376752380d/docs/TRADING_ANALYTICS_EXECUTION_POLICY_DRAFT.md)가 소유한다. 이 문서는 Compute의 현재 코드와 영향·
후속 작업을 연결하며 새로운 실행 제한을 확정하거나 구현하지 않는다.

## 1. 이번 단계의 작업과 범위

Context·Core·Compute에 독립적인 `main` 기준 문서 PR을 제출한다. Context는
`docs/DECISIONS.md`의 DRAFT 요약과 상세 정책 초안, Core는 그 문서의 동일한 snapshot,
Compute는 이 검토 문서와 작업 계획을 각각 관련 검증과 함께 커밋한다.
Core의 `openapi/CONTEXT_REVISION`은 복제한 문서의 실제 Context commit을 기록하는 metadata다.
실행 코드·OpenAPI schema·migration·fixture·설정 기본값은 바꾸지 않는다.
AR-07 PR과의 미머지 변경이 섞이지 않도록 별도 worktree를 사용한다.

## 2. 현재 코드에서 확인한 사실

| 코드 경계 | 현재 동작 | 정책상 의미 |
|---|---|---|
| `application/analytics_worker.py` | 부모 안에서 fetch·adapt·calculate·projection·직렬화, 별도 heartbeat thread | 계산이 멈춰도 lease가 계속 갱신될 수 있음 |
| 같은 worker의 `run_once` | lease 60초, heartbeat 20초·finally join 1초 | heartbeat는 실행 deadline이 아님 |
| `adapters/analytics/artifacts.py` | open/read socket timeout 20초·chunk별 expiry/stop 확인 | 전체 fetch·계산·부분 IPC 수신 한도는 아님 |
| `app/config.py` | artifact별 입력·운영 출력 기본 512MiB | JSON 객체·bytes 복사·암호화를 포함한 RSS 상한은 아님 |
| `application/analytics_result.py` | 결과 tree에서 bytearray를 쌓은 뒤 bytes 반환 | 결과 tree와 직렬화 사본이 겹칠 수 있음 |
| `persistence/postgres/trading_analytics_jobs.py` | body digest·AES-GCM 이후 payload/job/attempt 원자적 commit | child뿐 아니라 부모의 암호화·DB 예산도 필요 |
| claim·attempt schema | job started_at과 attempt created_at은 있으나 첫 claim 기준 attempt deadline은 없음 | reclaim마다 timeout을 새로 주면 같은 attempt가 반복될 수 있음 |
| worker claim query·exact version 검사 | queue는 digest로 routing하지 않고 claim 뒤 설정 digest와 대조 | 구·신 worker를 같은 queue에 섞는 배포는 안전하지 않음 |
| Trading Review failure enum | Analytics timeout·resource-limit code 없음 | Backtest EXECUTION_TIMEOUT을 그대로 사용할 수 없음 |

Job Protocol V1은 최대 3 attempts·24시간 cutoff·Core만 retry·grant 갱신 CAS를 이미 정한다.
계산·표본·Evidence 의미는 ADR-060, publication·retention은 ADR-062/063/069를 보존한다.
Core 삭제의 실시간 generation을 Compute가 직접 조회한다고 가정하지 않는다.

## 3. ADR 초안의 권장 방향

- 부모는 lease·heartbeat·현재 claim의 DB 전이, 자식은 격리된 계산을 소유한다.
- 첫 claim 기준 deadline을 attempt에 영속화하고 같은 attempt의 재시작에도 남은 예산만 쓴다.
  큐 대기와 retry 대기는 제외하되 spawn·fetch·검증·계산·projection·직렬화·완전한 결과 수신과
  자식 종료 확인까지는 포함한다. 완전한 결과 확인은 deadline보다 엄격히 빨라야 한다.
- 결과가 READY인 뒤 암호화·DB 발행은 별도의 유한한 부모 예산으로 제한한다. Timeout의
  계산 구간과 Core Run 전체 응답 SLA를 혼동하지 않는다.
- 제안된 `ANALYTICS_EXECUTION_TIMEOUT`·확인된 `ANALYTICS_RESOURCE_LIMIT_EXCEEDED`는
  nonretryable이다. 원인 불명 child crash는 기존 일시 실패 taxonomy와 Core retry budget을 따른다.
- 취소·lease/token/generation은 DB의 기존 조건부 전이를 우선한다. 늦은 성공뿐 아니라 늦은
  실패도 이전 소유자가 기록하지 못하게 한다. Core의 삭제·stale discard는 별도로 유지한다.
- 큰 dict와 bytes를 pickle로 통째 수신하는 대신 제한된 제어 outcome·비공개 bounded spool을
  우선 검토한다. 부분 frame이 부모를 멈추지 않아야 하며 spool 경로·내용은 공개하지 않는다.
- Child의 OS 한도, 부모의 암호화/DB peak, disk·worker 동시성을 각각 검증한다. 기본 지원
  production 대상은 Linux/systemd이며 Windows 개발 테스트를 메모리 격리 보장으로 해석하지 않는다.

이 항목들은 제안이다. 수치와 공개 failure mapping, server-side execution profile pin·운영
노출 방식은 미확정이다. 환경 변수나 policyVersion을 실제 구현에 추가한 것으로 읽지 않는다.

## 4. 수치·운영 profile gate

T(실행 시간), G(종료 유예), S(관측 간격), P(부모 발행 예산), child memory·parent reserve·
worker 동시성·control frame·spool quota는 대표 입력을 측정한 뒤 확정한다. Backtest의
600초·30초나 작은 Analytics fixed vector를 근거로 운영 기본값을 정하지 않는다.

측정은 최대 허용 입력, 많은 Evidence/관계, JSON 객체 증폭, 느린 stream, 최대 output,
AES-GCM·DB lock 경합·동시 실행을 포함한다. Bytes cap을 지키는 것과 RSS가 안전한 것을
따로 검증한다. 입력을 잘라서 성공하거나 광고된 상한을 조용히 낮추지 않는다.

## 5. 후속 커밋·PR 순서

| 순서 | 소유·범위 | 해당 PR의 검증 | 다음 단계에 남기는 연결 |
|---|---|---|---|
| AR-08 제안 | Context ADR DRAFT·Compute 영향 검토 | 문서 링크·현재 코드/정본 대조·계약 무변경 | 사람의 정책 리뷰·수치 측정/승인 |
| AR-08 승인·계약 | Context protocol/acceptance·Core/Compute enum와 공개 mapping | 독립 schema·고정 벡터·실제 serializer와 실패 DTO | 구현이 내보낼 수 있는 확정된 code·상태 |
| AR-09 저장 준비 | Compute additive 내부 profile pin·attempt deadline metadata | legacy rows·reclaim·새 attempt·CAS·rollback 실 DB | 기존 production 동작 유지, 새 bounded profile 미활성 |
| AR-09 실행 준비 | executor·bounded IPC/spool·OS 감독·parent publication port | 실제 spawn·정지·부분 frame·OOM 증거·최대 결과·cleanup | worker에 미연결 상태로 검토 |
| AR-10 수신·운영 선행 | Core Analytics dispatch/검증/ACK와 Integration 실제 harness | 실제 Core→Compute→terminal→Core publication/ACK·삭제·실패 | Compute 단독 ASGI/DB로 대체하지 못하는 E2E gate |
| AR-10 연결·전환 | 부모 worker 연결·설정·systemd·metric/runbook·admission 전환 | 실제 DB 경합·대표 자원 profile·cutover/rollback·E2E | 모든 release gate 이후에만 활성화 |

파일 종류로 구현·필수 테스트·문서를 분리하지 않는다. 각 준비 PR은 기존 경로를 깨지 않고
독립 검토 가능한 경계를 포함하며, 미연결·미활성인 경계를 본문에 적는다. 수치 승인 전에
release 기본값을 숨겨 넣지 않는다. 범위가 커지면 소유 서비스의 독립 capability로 더 나눈다.

## 6. 배포와 rollback에서 지킬 조건

신규 admission/dispatch를 중지하고 미전송·응답 유실 Job을 먼저 수렴시킨다. 기존 작업을 drain
또는 Core의 명시적 취소와 소유 프로세스 정리로 끝내고 active legacy Job 0을 확인한다.
이미 저장된 completed payload·ACK·조회·삭제는 유지한다. 이후 호환 계약·additive migration·
실제 새 digest·profile·검증된 Core/Compute 조합을 배포하고 E2E 뒤 접수를 재개한다.

새 artifact가 과거 implementationDigest를 사용하면 안 된다. 같은 전체 pin에서의 성공 결과
보존과, 새 digest를 포함하는 VersionSet·input/result hash의 배포 간 변화를 구분한다.
과거 결과·고정 vector를 새 digest에 맞춰 덮어쓰지 않는다.

Rollback은 bounded Job을 unbounded worker에 넘기는 동작이 아니다. 새 Job의 pin을 지원하는
조합으로 drain·취소하고 admission 중지 상태에서 전환한다. 기존 결과와 latest를 되돌려 고치지 않는다.

## 7. 검증과 완료 판정

이번 PR은 문서만 바꾼다. 새 enum·timeout·migration·OS 제한을 실행한 것으로 보고하지 않는다.
새 pytest나 운영 성능 수치를 만들지 않으며 문서 링크, code/contract 대조와 계약 동기화를 검증한다.
AR-08 제안의 완료와 정책 승인·AR-09 준비·AR-10 production 활성화는 각각 별도로 판정한다.

현재 Core checkout에는 Analytics 입력 생성·artifact/revision adapter가 있으나 실제 Analytics
dispatch/publication/ACK runtime E2E는 기존 검증 공백이다. Normalization·Backtest smoke나
Analytics Compute 모의/ASGI·DB 테스트를 그 완료 증거로 사용하지 않는다.

Context 초안이 승인되지 않았거나 운영 profile 수치·지원 OS·공개 failure mapping이 미확정이면
AR-08 전체는 완료로 체크하지 않는다. ADR의 AR08-01~15 제안은 구현된 테스트나 확정된
TR-ACC ID가 아니며, 승인 후 기존 acceptance ID를 보존하면서 매핑한다.

이번 문서 변경의 Determinism impact: none. Point-in-Time impact: none.
