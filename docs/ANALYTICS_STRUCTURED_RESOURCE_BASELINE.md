# AR-08 구조적 JSON·결과 생성 실측

2026-11-01 KST. **WSL 개발 실측 / 운영 수치 미확정 / production 미연결**.
ADR-071은 DRAFT다. 이번 자료는 AR-08~10 완료나 실행·메모리 한도 승인을 뜻하지 않는다.

## 1. 목적과 변경 범위

[HTTP 입력 실측](ANALYTICS_HTTP_RESOURCE_BASELINE.md)은 작은 JSON tree에 whitespace를 붙여
전송 바이트의 비용을 분리했다. 이번에는 기존 유효 합성 입력 생성기를 바꾸지 않고 episode·
record·Evidence·관계 후보 수를 늘려 read/parse→adapt→Engine→projection→hash→직렬화→
암호화 전체를 측정했다. HTTP는 기존 file stream 대체 방식이며 실제 Core/DB/TLS/IPC는 제외한다.

도구에 단계 시작/종료·누적 RSS·오류 class의 `sample-N-progress.json`을 추가했다.
임시 파일을 쓴 뒤 rename하며, 요청·거래 값·key·오류 메시지는 담지 않는다. Watchdog·child exit·
MemoryError 뒤에도 마지막 관측을 보존한다. 관측이 없거나 손상되면 unavailable로 표시하며,
마지막 단계가 실패 원인이나 OOM 증거였다고 추정하지 않는다. 같은 sample index는 재사용하지 않는다.
이는 개발용 진단 파일이며 production IPC/spool·worker 감독·공개 오류 code를 구현하지 않는다.

Root payload 조립도 별도 `payload_assembly` 단계로 관측한다. 완료 body의 생성·hash 의미는
유지하며, unit 검사는 관측 유무에 따른 전체 body/input/result hash·section count 동일성을 확인한다.

## 2. 환경과 재현 조건

- 기준 main `b9ae24d`, 도구·테스트·사용법 커밋 `910bf90`.
- Python 3.11.16, WSL2 Linux 5.15.146.1, x86_64/glibc 2.35, 논리 CPU 16개.
  실행 전 MemAvailable 약 6.88GiB인 공유 개발 환경이다. 운영 Linux/systemd 환경이 아니다.
- Lock SHA-256 `8b5735f18b372f4a64cc8c6c8ca1d87d8ba85e3af28ce2bade098e20a3fbc75c`.
  FastAPI 0.141.1·cryptography 50.0.2·psycopg 3.3.4·pytest 9.1.1을 lock과 대조했다.
- 새 Linux checkout의 runtime source 165개와 도구를 Windows와 LF/CRLF 정규화 후 대조했다.
  Source 지문은 `9502d0e98cf5ac453eb06cb1cfb2808cf6233ee25170d3b869f313cc3fd24f5d`,
  도구 지문은 `f0ecd0b35229149be411bf926286fcd8f1d642681d2fa491aa2a0f98df862657`다.
  실제 배포 implementationDigest는 아니다.
- Generator를 먼저 별도 프로세스에서 종료하고 각 반복을 fresh process로 실행했다.
  고정 job/time·합성 VersionSet으로 평문 body/hash를 비교하며 임시 암호화 key는 저장하지 않는다.
- 모든 실험 프로세스는 **RLIMIT_AS 2GiB**를 상속했다. 주소 공간 제한이지 RSS/cgroup 한도나
  운영 권장값이 아니다. 출력 보호값은 보통 512MiB, watchdog는 보통 120초이며 예외는 아래에 명시한다.
- 시간은 import 후 read부터 암호화 또는 거부까지의 3회 중앙값이다. 관측 I/O도 전체 시간에
  포함되므로 과거 도구와 개선율을 계산하지 않는다. 단계별 중앙값의 합도 전체 중앙값과 같지 않다.
  메모리는 import를 포함한 client의 누적 `ru_maxrss`이며 단계별 값을 더하지 않는다.

## 3. 결과

MiB=1,048,576 bytes다. 입력은 Ledger와 Manifest의 **합산** 크기다. x32는 record당 합성
source Evidence 참조 32개이며, 같은 입력의 반복은 실제 Engine·projection·hash·암호화를 모두 거친다.

| Profile | Episode / Evidence 참조 | 입력 MiB | 결과·표본 | 평문 MiB | 중앙값 초 | 최대 RSS MiB |
|---|---:|---:|---|---:|---:|---:|
| dense256 | 256 / 512 | 0.661 | 3회 완료 | 5.420 | 1.045 | 76.0 |
| evidence256x32 | 256 / 16,384 | 3.851 | 3회 완료 | 7.680 | 1.840 | 170.5 |
| evidence512x32 | 512 / 32,768 | 7.705 | 3회 완료 | 23.012 | 7.747 | 495.8 |
| evidence1024x32 | 1,024 / 65,536 | 15.440 | 3회 완료 | 76.631 | 38.850 | 1742.0 |
| cap512x32 | 512 / 32,768 | 7.705 | 3회 RESULT_PAYLOAD_TOO_LARGE | — | 7.491 | 468.6 |
| watchdog1024x32 | 1,024 / 65,536 | 15.440 | 1회 BENCHMARK_WATCHDOG | — | — | — |

6개 profile·16개 표본 중 완료 12개, 의도한 실패 4개다. 완료 profile마다 3회의 input/result/전체
body hash가 같고 ciphertext 길이는 평문+28 bytes였다. Ciphertext 자체는 nonce 때문에 비교하지 않는다.
후보 수는 각각 N²/4로 최대 **262,144개**, canonical record는 최대 **2,048개**다. AFTER_WINDOW
후보를 버리거나 계산·표본·Evidence 의미를 바꾸지 않았다. 여기서 완료는 도구의 계산 완료이며
실제 durable Job 발행이나 Core 장기 결과 저장·ACK 완료가 아니다.

가장 큰 완료 profile의 단계별 중앙값과 각 단계까지의 최대 누적 RSS는 다음과 같다.

| 경계 | 중앙값 초 | 최대 누적 RSS MiB |
|---|---:|---:|
| Read·검증·strict parse | 0.116 | 106.7 |
| Adapt | 0.305 | 107.4 |
| Engine | 30.560 | 1363.0 |
| Projection | 2.109 | 1485.5 |
| Section hash | 3.842 | 1627.0 |
| Payload root 조립 | 0.000074 | 1627.0 |
| 직렬화 | 1.709 | 1664.6 |
| Body hash | 0.040 | 1664.6 |
| AES-GCM | 0.067 | 1742.0 |

## 4. 실패 관측과 구조적 의미

1. 입력 **15.44MiB**, 평문 **76.63MiB**에서도 전체 RSS **약 1.70GiB**를 관측했다.
   직렬화 전 Engine에서 이미 누적 1363MiB에 도달했다. Bytes cap을 RSS guard로 해석할 수 없으며,
   작은 입력의 배수로 최악의 상한을 보장하거나 같은 값을 미래 child/parent 한도로 나눌 수 없다.
2. cap512x32는 출력 보호값만 1MiB로 낮췄다. 실제 serialization에서 기존 오류로 거부됐고
   마지막 관측은 `serialization / FAILED / AnalyticsContractError`였다. 거부 전 **468.6MiB**를
   사용했으며 body hash·암호화·발행을 진행하지 않았다. 이 실험 설정을 운영 기본값에 반영하지 않는다.
3. Watchdog는 20초에 측정 자식을 종료하고 나머지 반복을 중단했다. 마지막 관측은
   `engine / RUNNING`, 시작 후 약 0.419초의 snapshot이었다. 이는 **종료 시 peak RSS가 아니다**.
   부모가 관측하지 못한 구간의 peak나 OS OOM을 만들어 넣지 않는다. Production timeout·
   lease/cancel 감독은 검증하지 않았다. 실제 MemoryError·예상 밖 crash/OOM 표본은 없으며,
   MemoryError 분류는 주입 테스트로만 확인했다.
4. 512MiB 결과 전체나 512MiB 구조적 JSON을 검증한 자료가 아니다. 생성기의 1,024 episode·
   Evidence 32개는 실험 보호 범위이며 계약상 admission 한도가 아니다. 범위를 줄이거나 후보를
   잘라 성공시키지 않았다. 실제 Core 전송·DB 발행·동시 worker·OS 격리·부모 reserve는 남아 있다.

## 5. 검증·재현·증적

- 새 진단 검사 8개, Windows 관련 **145 passed**, 전체 **932 passed / 131 skipped**,
  기존 AnyIO assert rewrite 경고 1개. Native Linux 관련 **145 passed**.
- Ruff·formatter 275 files·src strict Pyright 0 errors/0 warnings. Pyright 새 버전 안내는 있었으나
  lock·설치 버전은 바꾸지 않았다. Windows 관련/전체 회귀는 기존 외부 IPv6 bootstrap을 사용했고,
  새 8개 단독 검사와 Linux 검증·실측은 사용하지 않았다. Skip은 통과로 세지 않는다.
- `run_child`·`process_memory`의 AST, runtime source·설정·계약·migration·lock·고정 vector·입력
  생성기·앞선 HTTP 도구를 보존했다. Production 동작을 바꾸지 않아 실 DB·Core E2E·계약 동기화를
  이번 검증으로 새로 실행했다고 주장하지 않는다.

도구 사용법은 [tools 안내](../tools/README.md)를 따른다. 별도 실험 환경에서 예를 들면:

```sh
(
  ulimit -v 2097152
  PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m tools.analytics_resource_baseline run \
    /var/tmp/analytics-structured-512 --episodes 512 --evidence-per-record 32 \
    --max-payload-bytes 536870912 --watchdog-seconds 120 --repetitions 3
)
```

Raw 입력·report·progress·로그·JUnit은 실험 ID `20261031T160630Z`의 Linux 전용 폴더와 저장소
밖 `refinvest-ar08-structured`에 보존한다. 측정 자식·프로세스 그룹은 종료됐고 source도 다시 대조했다.
Credential·실측 산출물·기존 PR_BODY.md는 커밋하지 않는다.

다음 gate는 운영 사양과 지원할 구조적 입력/출력·동시성 범위, 실제 DB 발행·부모 예산 실측 및
정책 승인이다. [ADR-071 검토](ANALYTICS_EXECUTION_POLICY_REVIEW.md)의 수치·계약 gate를 거친 후
격리 executor·production 연결을 별도 PR로 검증한다. AR-08~10 체크박스는 계속 미완료다.

Determinism impact: none. Point-in-Time impact: none. Temporal/Execution impact: none.
