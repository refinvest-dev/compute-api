# AR-08 실행 정책 결정 검토

2026-11-01 KST, Compute main `b3833ee` 기준. **검토 자료 / 수치 미확정 / production 미연결**.
이 문서는 실측 근거와 다음 결정을 연결한다. 정책 정본은
[Context ADR-071 DRAFT](https://github.com/refinvest-dev/context/blob/9364ecb3cc9bbde929fdd4e3762b35c4a2408b2e/docs/TRADING_ANALYTICS_EXECUTION_POLICY_DRAFT.md)다.
현재 ADR은 미승인이며 이 문서나 PR의 머지를 정책 승인으로 해석하지 않는다.
새 enum·설정·migration·executor·운영 기본값은 추가하지 않는다.

## 1. 이번 검토의 결론

추가 소규모 benchmark보다 **정책 결정과 운영 환경 확인**을 먼저 진행할 시점이다.
기존 관측은 bytes cap·socket timeout·lease가 실행 시간이나 전체 메모리를 제한하지 못한다는
위험과 descriptor/ACK의 국소 비용을 확인했다. 운영 T/G/S/P·메모리·동시성 값을 결정할
최대 workload·실제 OS 감독·전체 worker 자원 증거는 아직 갖추지 못했다.

권장 방향은 ADR 초안의 부모 감독·attempt deadline 보존·bounded spool 검토다. 아래는
**권장 검토안**이며 확정된 정책이 아니다. 기존 Job Protocol V1·Result Delivery V1은 유지한다.

| 결정할 항목 | 권장 검토안 | 정본 근거·남은 확인 |
|---|---|---|
| 실행 소유권 | 자식은 입력 검증·계산·직렬화, 부모만 heartbeat·취소·조건부 terminal write 소유 | ADR-071 §2.1; DB/key를 인자에서 뺀 것만으로 환경·동일 UID까지 격리됐다고 주장하지 않음 |
| Deadline T | 첫 유효 claim의 DB 시각으로 attempt deadline 고정, 같은 attempt reclaim은 남은 예산만 사용 | §2.2~2.3; spawn~완전한 결과 수신·검증과 child 종료가 D보다 먼저 완료되어야 READY |
| 발행 예산 P | READY 이후 암호화·connection·lock·statement·commit/abort는 별도 유한 예산 | §2.3; heartbeat 여유·DB clock 사용 지점·실제 CAS와 rollback을 함께 검증, P가 lease보다 작다는 비교만으로 안전 판정하지 않음 |
| 실패·재시도 | 초안의 timeout·증거가 있는 child resource-limit은 nonretryable; 원인 불명 crash와 구분 | §3; 제안 code는 아직 enum에 없으므로 사용 금지, Core 공개 mapping·기존 retry budget의 계약 리뷰 필요 |
| 경합 | 소유권·token/generation·lease와 DB에 commit된 상태를 우선; 늦은 성공·실패 모두 차단 | §3; Core tombstone을 실시간 조회한다고 가정하지 않으며 기존 stale discard 책임 보존 |
| 결과 전달 | 부모 소유의 비공개 bounded spool과 작은 제어 outcome을 우선 검토 | §4; 임의 child path·큰 pickle 수신에 의존하지 않음, 부분 frame·EOF·부모 crash·orphan·key/handle 수명 검증 필요 |
| OS·메모리 | 첫 production 검토 대상은 Linux/systemd, child 한도와 부모 생존·암호화 reserve를 별도 증명 | §4~5; 전체 unit 한도만으로 child OOM에서 heartbeat 부모가 살아남는다고 보장하지 않음 |
| Profile·배포 | Job에 고정된 server-side profile과 실제 새 artifact digest, admission 중지·drain/취소 후 전환 | §5~6; 현재 wire/DB에 없는 제안 metadata, legacy deadline 소급 부여와 구·신 worker의 무검증 queue 혼용 금지 |

이 표는 ADR의 규칙을 대체하거나 새로운 acceptance ID를 부여하지 않는다. 세부 우선순위와
경계는 정본을 따른다. 정책 리뷰·수치 승인·계약 반영·production 활성화는 각각 기록하되,
일부 검토 완료를 정본 §8의 전체 gate 통과로 바꾸지 않는다.

## 2. 현재 증거로 말할 수 있는 범위

| 근거 | 확인한 사실 | 이 자료로 확정할 수 없는 값 |
|---|---|---|
| [HTTP 입력 실측](ANALYTICS_HTTP_RESOURCE_BASELINE.md) | WSL에서 512MiB 두 artifact의 검증 client peak 1055.3MiB; trickle fetch 26.232초 | 전체 worker RSS·구조적 JSON 상한·production fetch tail, T나 child memory |
| [구조적 JSON 실측](ANALYTICS_STRUCTURED_RESOURCE_BASELINE.md) | WSL 1,024 episode·65,536 Evidence: 입력 합계 15.440MiB, 출력 76.631MiB, pipeline 중앙값 38.850초·peak 1742.0MiB | 허용 최대 입력/출력의 성공 범위·운영 tail·미래 child/parent 분리 peak |
| 같은 실측의 cap·watchdog | 직렬화 cap 이전에 큰 객체를 보유하며, 종료 후 마지막 관측을 보존 | watchdog를 production deadline으로, 마지막 snapshot을 종료 시 peak/OOM 증거로 해석할 수 없음 |
| [실제 DB 발행 실측](ANALYTICS_PUBLICATION_RESOURCE_BASELINE.md) | 실제 terminal commit·roundtrip·ACK, 2초 row lock 대기·별도 schema 두 publisher 검증 | P의 운영값·장기 DB 장애·shared queue의 전체 worker 동시성 |
| [조회 최적화 전후](ANALYTICS_DELIVERY_QUERY_OPTIMIZATION.md) | Windows 18.489MiB 결과의 descriptor 0.512→0.115초, ACK 0.537→0.199초; 전체 client peak 231.6→232.4MiB | 전체 worker 메모리가 줄었다는 결론이나 새 운영 메모리 한도 |
| [Linux 검증](ANALYTICS_RESOURCE_LINUX.md) | WSL의 실제 POSIX process-group 정리·resource 관측 | RLIMIT_AS 2GiB는 RSS/cgroup 한도가 아니며 production systemd의 OOM 격리를 증명하지 않음 |

각 행은 서로 다른 source·OS·Python·측정 경계의 과거 증거다. 해당 문서의 commit·환경·실패
기록을 함께 사용하고 시간을 합치거나 RSS를 더해 새 worker 예산을 만들지 않는다. 소수 반복의
중앙값을 p95/p99나 SLA로 바꾸지 않는다. 정상 입력을 잘라서 성공시키거나 광고된 cap을
조용히 낮추는 방식은 검토안에 포함하지 않는다.

## 3. 배포 템플릿과 실제 운영 환경을 구분한다

저장소의 [Analytics unit](../deploy/systemd/refinvest-trading-analytics-worker.service)과
[환경 예제](../deploy/systemd/trading-analytics-worker.env.example)에서 확인한 사실은 다음과 같다.
배포 서버의 실제 unit·drop-in·컨테이너/cgroup 설정이나 현재 가동 수를 조회한 것은 아니다.

| 저장소에서 확인한 값 | 의미·오해하면 안 되는 점 |
|---|---|
| Linux 경로의 systemd `Type=exec` 진입점 | 배포 예제의 대상; 실제 운영 OS·kernel·systemd/cgroup 버전은 미확인 |
| `TimeoutStopSec=10m` | unit stop의 대기 설정이며 Analytics attempt의 실행 deadline T가 아님 |
| `Restart=on-failure`, `RestartSec=30s` | service 재시작 정책이며 새 attempt나 실행 예산 reset의 근거가 아님 |
| `REFINVEST_WORKER_POLL_SECONDS=1` | queue polling 예제이며 child 실행 중 stop 관측 간격 S의 보장이 아님 |
| `REFINVEST_TRADING_REVIEW_ACTIVE_JOB_LIMIT=20` | Normalization/Analytics의 admission pool 예제이며 worker 20개나 동시 실행 수가 아님 |
| 입력 artifact별·결과 예제 cap 각 512MiB | 전체 RSS 한도·지원 workload 복잡도·spool disk 보장이 아님 |
| 이 unit에 `MemoryMax`·`MemoryHigh`·`RuntimeMaxSec` 명시 없음 | checked-in 템플릿만의 사실; 실제 서버의 다른 한도 유무를 단정하지 않음 |

현재 application worker의 lease 60초·heartbeat 20초도 소유권 갱신의 기준이다.
계산이 멈춰도 heartbeat가 살아 있으면 갱신될 수 있으므로 이를 hard deadline으로 해석하지 않는다.
실제 운영 환경 정보는 요청했으나 이번 검토 시점에는 아직 제공되지 않았다. EC2 type·vCPU/RAM·
worker 수나 공존 여부를 예제 값으로 채우지 않는다.

## 4. 운영 profile을 채우기 위한 입력과 증거

| 결정에 필요한 입력 | 이번 상태 | 확인할 산출물·소유 |
|---|---|---|
| 실행 환경 | 미확인 | 운영 담당: OS·kernel·systemd/cgroup 또는 컨테이너 runtime·실제 배포 revision |
| Host/할당 자원 | 미확인 | 운영 담당: vCPU/RAM·swap 정책·container/cgroup 한도·임시 disk 여유·Core/DB 공존 여부 |
| 동시 실행 계획 | 미확인 | 운영 담당: Analytics·Normalization·Backtest·API·ingestion의 실제 process 수와 동시성, 공존 서비스 예산 |
| 지원 workload와 성공/실패 기대 | 계약 cap 외 운영 envelope 미확정 | Core/제품·Compute: record/Evidence/관계 분포·최대 entity·느린 storage, 제한 초과 시 허용되는 실패 의미 |
| T와 P | 미확정 | Compute/운영: 대상 OS·동일 배포 artifact에서 전체 단계와 DB 지연·중단/rollback 관측 |
| G·S | 미확정 | Compute/운영: 막힌 I/O·부분 제어 frame·SIGTERM/SIGKILL·descendant/부모 crash의 종료 지연 |
| Child memory·parent reserve | 미확정 | Compute/운영: 동시 생존하는 parent/child·검증/staging·AES-GCM·DB 사본, 한도 초과 증거와 부모 생존 |
| 제어 frame·spool quota | 미확정 | Compute/운영: 정상 최대 metadata·entity·동시 작업·disk full·orphan 수와 cleanup/보관 수명 |

Host 예산은 OS·API·공존 서비스와 실제 동시 worker group을 포함해야 한다. 서로 다른 실험의
개별 최대 RSS를 단순 합산하거나 child만 제한한 값을 전체 worker 예산으로 쓰지 않는다.
같은 경계에서 동시 점유를 계측하고, reserve·hard limit·실패 분류·부모 생존을 함께 확인한다.
최대 입력의 모든 경우가 성공한다는 보장과 자원 한도 초과를 명시적으로 실패시키는 정책도
구분해 리뷰해야 한다. 현재 자료만으로 임의의 T/G/S/P 또는 GiB·worker 수를 권장하지 않는다.

## 5. 다음 작업의 순서와 진입 조건

| 순서 | 작업·산출물 | 완료 조건 |
|---|---|---|
| 이번 PR | Compute의 결정 검토·근거·운영 입력·미완료 gate를 한곳에 연결 | 원본 실측·정본 대조와 링크·변경 범위 검증; 정책 승인·runtime 변경 없음 |
| 정책/운영 리뷰 | 위 권장안의 채택·수정·보류 사유, 운영 입력과 필요한 실험을 기록 | 소유자의 명시적 결론과 정본 §8 gate별 상태; PR 머지만으로 APPROVED 표시하지 않음 |
| 정본·호환 계약 | Context 정책/acceptance와 Core/Compute 실패 enum·안전한 공개 mapping | 정본의 승인 조건 충족 후 별도 PR, 기존 identity/result vector와 실제 실패 DTO 검증·snapshot 동기화 |
| AR-09 저장 준비 | 승인된 profile pin·attempt 시작/deadline의 additive metadata | 기존 row·같은 attempt reclaim·새 retry·CAS·rollback 실 DB 검증, 새 production 경로 미활성 |
| AR-09 실행 준비 | 승인된 감독·bounded outcome/spool·부모 publication 경계 | 실제 spawn·정지·부분 frame·crash·OS limit·큰 entity·cleanup 검증, worker 미연결 |
| Core/Integration·AR-10 연결 | 실제 Analytics dispatch→검증→Core durable publication→ACK와 부모 worker·운영 전환 | 실제 E2E·DB 경합·대상 OS 자원·drain/rollback·모든 활성화 gate 통과 |

저장 준비와 실행 준비를 문서 예시만 보고 선제 구현하지 않는다. 다음 구현 범위는 그 시점에
승인된 정본과 의존성을 확인해 다시 나눈다. 기존 Normalization/Backtest smoke·Compute ASGI/DB
회귀를 실제 Core Analytics E2E의 완료로 쓰지 않는다. AR-08~10 체크박스는 이번 PR에서 닫지 않는다.

## 6. 이번 문서의 검증 범위

현재 code·systemd/env 예제·OpenAPI enum·Context DRAFT 및 각 실측 기록을 대조한다.
Markdown 링크와 기존 tracked runtime/test/tool/fixture·계약·migration·deploy·lock 및 로컬
`PR_BODY.md`의 보존을 확인한다. 새 code나 수치를 도입하지 않아 pytest·DB·Core smoke·실측을
다시 실행한 것으로 보고하지 않는다. 직전 구현 PR의 958 passed/157 skipped·실 DB 70개·
전후 36개 client는 그 PR의 과거 증거다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
