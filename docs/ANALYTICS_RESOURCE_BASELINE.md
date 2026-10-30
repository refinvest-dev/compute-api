# AR-08 Analytics 자원 실측 — 1차 합성 profile

2026-10-30. **측정 자료 / 운영 수치 미확정 / production 미연결**.
정책의 소유자는 [Context ADR-071 DRAFT](https://github.com/refinvest-dev/context/blob/fb741006cb51f36dbb76ebd20c009a376752380d/docs/TRADING_ANALYTICS_EXECUTION_POLICY_DRAFT.md)다.
이 결과는 초안의 승인이나 AR-08~10 완료를 의미하지 않는다.

입력 바이트 cap만으로 계산량·출력 크기·프로세스 메모리를 제한할 수 없음을 확인했다.
512 episode의 합산 입력 약 1.32MiB에서 후보 65,536개·출력 18.49MiB가 생겼고,
read부터 암호화까지 24.35~39.21초, 관측 최대 working set 194.6MiB였다.
이 작은 로컬 profile로 T/G/S/P·메모리·동시성을 production 기본값으로 정하지 않는다.

## 1. 기준과 측정 방법

- 최초 Windows 측정 source 기준: `111f1cca61450853aaec8d28c4d17928a327106e`.
  그 측정 당시 AR-07 PR #116은 미머지 상태였으며 그 diff를 포함하지 않았다.
- 최초 도구/검증 commit: `430534d`. 아래 7개 profile은 이 도구로 각각 3회 실행했다.
  후속 변경은 측정용 watchdog의 하위 프로세스 정리 보강이며 서비스 소스를 바꾸지 않는다.
- 모든 profile의 `src/**/*.py` 경로·내용을 정렬해 계산한 source SHA-256:
  `08fb70e6c677ffe6c94df81c82ef1e00ef2fcbb73b43225202e307d40bebe6e7`.
  이는 측정 대상 source fingerprint이며 production `implementationDigest`가 아니다.
- Windows build 26200, CPython 3.11.5 / 64 bit, Intel Family 6 Model 186 Stepping 2,
  논리 CPU 16개. 공유 개발 호스트이며 전용 Linux 배포 환경이 아니다.
- 합성 입력 생성 프로세스를 끝낸 뒤 매 반복마다 새 Python 프로세스를 생성했다.
  이전 반복의 객체·generator 메모리를 재사용하지 않았다. HTTP opener만 파일 stream으로
  대체하고 실제 chunk·size/hash 검사, strict parser, adapter·Engine·projection·hash·
  bounded JSON·AES-GCM을 실행했다. 준비·실측 방법은 [도구 안내](../tools/README.md)를 따른다.
- Pipeline 시간은 import 후 read/검증부터 암호화 종료까지다. Process 시간은 생성부터
  종료까지이며 import도 포함한다. 둘 다 DB transaction·Core 전달·실제 network는 제외한다.
- Peak RSS는 OS `PeakWorkingSetSize`, peak commit은 `PeakPagefileUsage`다.
  아래 메모리는 단계별 증가량이나 Python heap이 아닌 **프로세스 전체의 누적 최대치**다.
  Tracemalloc을 사용하지 않아 그 계측 비용은 없지만 import·OS 계측·공유 호스트 영향은 있다.
- 표의 시간은 3회의 중앙값과 범위, 메모리는 3회 중 최대치다. 표본 3개로 p95/SLA를 추정하지 않는다.
  Phase별 중앙값의 합도 전체 중앙값과 같다고 가정하지 않는다.

## 2. 합성 입력과 결과

모든 profile은 한 instrument의 중첩 없는 LONG OPEN/CLOSE, 손실·이익 교대, episode당
canonical execution 2개를 사용한다. IDs·Evidence 링크는 고유하며 ledger/manifest·artifact·
config/input hash를 다시 만들었다. 고정 vector와 실제 upstream 산출물을 구분한다.
Opaque Normalization pin과 VersionSet은 원본 vector의 **합성값**이다. 실제 Normalization→Core
artifact 생성이나 실제 배포 digest의 정합성을 검증한 자료로 읽지 않는다.

| Profile | Episode | 간격 / Evidence 참조 | 입력 합계 MiB | 후보 수 | 결과 MiB | Pipeline 중앙값 [범위] 초 | 최대 RSS / commit MiB |
|---|---:|---|---:|---:|---:|---|---|
| dense32 | 32 | 2초 / 1 | 0.085 | 256 | 0.265 | 0.119 [0.116~0.203] | 66.6 / 52.7 |
| dense128 | 128 | 2초 / 1 | 0.332 | 4,096 | 1.757 | 1.534 [1.479~2.127] | 79.2 / 64.7 |
| dense256 | 256 | 2초 / 1 | 0.661 | 16,384 | 5.420 | 7.378 [4.377~9.666] | 106.2 / 91.6 |
| dense512 | 512 | 2초 / 1 | 1.319 | 65,536 | 18.489 | 36.221 [24.348~39.214] | 194.6 / 181.1 |
| spaced256 | 256 | 1시간 / 1 | 0.661 | 16,384 | 4.777 | 2.550 [2.459~2.651] | 102.9 / 89.0 |
| evidence256x8 | 256 | 2초 / 8 | 1.381 | 16,384 | 5.931 | 3.720 [3.618~3.721] | 126.7 / 112.5 |
| cap256 | 256 | 2초 / 1, 출력 cap 1MiB | 0.661 | 16,384 | 발행 없음 | 3.041 [2.749~3.546] | 94.8 / 81.5 |

MiB는 1,048,576 bytes다. Evidence 참조는 canonical record당 수다. 앞 6개 profile은
기존 API 의미의 `COMPLETED` 결과를 구성했고, 고정 job/time에서 각각 3회의 전체 평문
body SHA-256이 같았다(총 18회). Ciphertext의 nonce는 매번 달라 결정론 비교에서 제외한다.
앞 4개와 evidence profile은 세 metric 모두 AVAILABLE이다. Spaced profile은 reentry·holding이
AVAILABLE, exposure는 window 내 pair가 없어 INSUFFICIENT_SAMPLE이다. Finding은 V1에 따라 0개다.

`cap256`은 3회 모두 기존 `RESULT_PAYLOAD_TOO_LARGE`로 끝났고 body 발행·암호화를 진행하지 않았다.
성공 결과가 없으므로 도구의 `determinism_passed`도 false다. 이 실패를 성공 표본에 섞지 않았다.
나머지 profile의 측정용 출력 cap은 64MiB, watchdog는 60초였으며 도달하지 않았다.
이 값은 실험 보호값이고 production 권장값이 아니다.

Dense512의 fresh process 전체 시간 중앙값은 39.287초였다. Pipeline 단계별 중앙값은 다음과 같다.

| 경계 | 초 |
|---|---:|
| File read·content 검증·strict parse | 0.025 |
| Ledger/manifest 검증·adapt | 0.149 |
| Engine | 12.475 |
| Projection | 16.267 |
| Section integrity/hash | 4.268 |
| Bounded JSON 직렬화 | 2.695 |
| Body SHA-256 | 0.023 |
| AES-GCM | 0.034 |

시간 편차와 실행 순서·호스트 부하가 통제되지 않았다. 예를 들어 Evidence 8배 profile이
dense256보다 빨랐다는 관측을 Evidence 추가의 성능 개선이나 정확한 비용 차이로 해석하지 않는다.
별도 실행의 시간 비율로 복잡도 차수를 확정하지 않는다.

## 3. 확인된 위험과 구조적 함의

1. **관계 cardinality를 별도로 봐야 한다.** 이번 짝수 N profile의 후보 수는 정확히 N²/4다.
   Engine `_build_reentry`가 손실마다 entry를 순회하고 window 뒤 후보도 감사 근거로 남긴다.
   Spaced profile에도 16,384개가 생겼다. 단순히 30분 window 탐색으로 바꿔 AFTER_WINDOW
   후보를 버리는 최적화는 결과 계약을 바꾸므로 허용하지 않는다.
2. **Projection도 중요한 비용이다.** `build_sections`는 관계·exposure observation을 구성하면서
   `_scalar_inputs` 결과를 반복해서 만들고 필터링한다. Dense512에서 관측된 projection
   중앙값은 Engine보다 컸다. 한 invocation 안에서 동일한 scalar 목록·unit별 index를 재사용하는
   후보를 별도 PR에서 profile하고 전체 body/result hash와 고정 vector로 보존 여부를 검증할 수 있다.
   이 단계에서는 최적화하거나 의미를 바꾸지 않았다.
3. **출력 cap은 RSS guard가 아니다.** 1MiB cap 실패 전에도 Engine·projection·section hash는
   완료돼 tree가 이미 만들어졌다. Import를 포함한 peak RSS는 94.8MiB였다. 기본 artifact별
   512MiB를 전체 프로세스 메모리 한도로 해석하거나 작은 측정 배수를 곱해 상한을 보장하면 안 된다.
4. **부모 예산을 없앨 수 없다.** 이번 크기에서 AES-GCM 시간은 작았지만 실제 whole-body bytes·
   ciphertext를 겹쳐 보유했다. 현재 구조의 한 프로세스 peak를 미래 child memory와 parent reserve로
   정확히 나눌 수 없다. IPC/spool·부모 staging·DB 전달을 연결한 뒤 각 프로세스를 다시 측정해야 한다.
5. **실행 budget에는 계산 외 비용도 포함해야 한다.** Import/spawn과 read·검증·결과 구성·hash·
   직렬화 비용을 포함한 ADR 초안의 T 경계가 필요하다는 근거다. Network·IPC·DB가 빠진 수치를
   end-to-end SLA나 운영 timeout으로 승인하지 않는다.

## 4. 아직 결정할 수 없는 값과 후속 순서

T/G/S/P, child hard limit·parent reserve·control frame·spool quota·worker 동시성은 **모두 미확정**이다.
상한을 조용히 낮추거나 입력/후보를 잘라 성공시키는 정책도 추가하지 않았다.

1. 지원 Linux/systemd·실제 배포 Python/의존성·메모리 격리 환경에서 같은 입력을 재측정한다.
   대표 production 분포와 긴 ID·많은 증거/관계·최대 허용 **각 artifact 512MiB / 협상 출력**을
   별도로 검증한다. 이번 최대 합산 입력 1.381MiB로 그 검증을 대체하지 않는다.
2. 승인된 성공 결과 의미를 보존하는 projection 재사용·index 후보를 독립 PR로 검토한다.
   같은 전체 pin에서 입력/결과/body hash·모든 regression·전후 동일 fixture를 검증한다.
   실제 새 배포 artifact에는 실제 새 digest를 사용하며 과거 golden pin을 덮어쓰지 않는다.
3. 느린/trickle 전송, DB lock·publication 지연, 다중 worker의 peak, OS OOM·kill·종료 유예,
   부분 IPC/spool·cleanup을 해당 구현 단계에서 계측한다. 기존 API active-job pool과
   동시 실행 프로세스 수를 구분한다. 전용 환경의 반복 표본과 운영 여유량으로 수치안을 만든다.
4. Context에서 수치·오류 mapping·profile pin을 리뷰/승인하고 계약을 반영한다. 이후 additive
   attempt metadata, 미연결 executor, 실제 Core dispatch/publication/ACK harness, production 연결·
   전환을 [기존 순서](ANALYTICS_EXECUTION_POLICY_REVIEW.md)대로 진행한다.

Production OS/컨테이너·Python/의존성·Engine/projection·입출력 상한·동시성 변경 시 재측정한다.
제한 정책이나 실행 구조가 바뀔 때는 timeout·lease/cancel·late publication 회귀를 재검증한다.
실제 Core E2E는 fixture/unit 검사나 Backtest smoke로 대체하지 않는다.

## 5. 검증과 증적

생성 입력의 Create 요청·Ledger·Manifest·완성 payload OpenAPI schema, 실제 adapter의 hash·
Evidence 검증, 정상 metric·후보 수·반복 body 결정론, 변조 거부·출력 cap을 테스트한다.
측정용 watchdog는 실제 자손 프로세스가 시작된 뒤 종료되는지도 검사한다. Windows에서는
프로세스 정리가 허용되는 실행 권한이 필요하며 sandbox 거부를 통과로 처리하지 않는다.
Ruff/formatter, 기존 `src` strict Pyright와 전체 regression 결과를 PR에 기록한다.

최초 Windows 도구 검증은 Ruff·formatter 257 files, strict Pyright 0 errors/0 warnings,
전체 pytest **668 passed / 131 skipped / 기존 AnyIO assert-rewrite warning 1개**다.
Windows 네트워크 회귀에는 이전 단계의 저장소 밖 IPv6 loopback bootstrap을 사용했다.
Watchdog 보강 후 CLI도 새 프로세스로 2회 실행해 기존 dense32 전체 body hash와 일치함을 확인했다.

Raw request/artifact/report·test log는 로컬 임시 폴더 `refinvest-ar08-measurements`에 보존했다.
재생성 가능한 합성 자료지만 Git에 커밋하지 않는다. API·worker·계산·persistence·OpenAPI·
migration·golden fixture는 이 PR에서 변경하지 않으며 DB/Core E2E를 새로 실행했다고 주장하지 않는다.

Determinism impact: none. Point-in-Time impact: none.

## 6. Linux 후속 검증

현재 main의 projection 개선을 포함한 [Linux 실측](ANALYTICS_RESOURCE_LINUX.md)을 추가했다.
WSL의 전체 689개 테스트와 8개 profile 24개 표본을 검증하고, 완료 표본의 Windows hash 일치를
확인했다. 위 표는 최초 Windows 측정 기록이며 Linux 데이터나 현재 source의 성능으로 덮어쓰지 않는다.
