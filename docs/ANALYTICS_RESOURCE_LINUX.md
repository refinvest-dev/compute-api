# AR-08 Linux 자원 실측

2026-10-30. **WSL Linux 검증 자료 / 운영 수치 미확정 / production 미연결**.
[ADR-071 DRAFT](https://github.com/refinvest-dev/context/blob/fb741006cb51f36dbb76ebd20c009a376752380d/docs/TRADING_ANALYTICS_EXECUTION_POLICY_DRAFT.md)의
승인이나 production Linux/systemd 격리 검증을 대신하지 않는다.

이전 [Windows 기초 실측](ANALYTICS_RESOURCE_BASELINE.md) 이후 main에 머지된
[projection 개선](ANALYTICS_PROJECTION_SCALARS.md)을 포함해 Linux에서 같은 합성 입력을 실행했다.
검증한 source는 main `b261f838915ad494cc8b17a36a6aabcef161a53a`, 측정 도구는 `656c83e`다.
PR #118에는 현재 main을 일반 merge한 뒤 이 자료를 추가하며 production source는 수정하지 않는다.

## 1. 환경과 재현 조건

- 기존 Ubuntu WSL2, kernel `5.15.146.1-microsoft-standard-WSL2`, x86_64, glibc 2.35.
  논리 CPU 16개, `/proc/meminfo`의 총 메모리 약 7.54GiB인 공유 개발 환경이다.
- checksum을 확인한 uv 0.12.5로 별도 검증 폴더에 CPython **3.11.16**과 가상환경을 준비하고
  `uv sync --locked --dev --python 3.11`을 실행했다. System Python 3.10과 구분한다.
- 실제 사용한 lockfile SHA-256은 `8b5735f18b372f4a64cc8c6c8ca1d87d8ba85e3af28ce2bade098e20a3fbc75c`다.
  Git blob의 LF/CRLF 정규화 후 내용이 일치함을 확인했다. 주요 버전은 FastAPI 0.141.1,
  cryptography 50.0.2, psycopg 3.3.4, pytest 9.1.1, Ruff 0.16.4, Pyright 1.1.411이다.
- main의 `git archive`와 도구 파일을 Linux 파일시스템의 고유 검증 디렉터리에 조립했다.
  `.env`·PR_BODY.md·실제 DB/vendor credential을 사용하지 않았다. Production Python 파일
  160개의 LF/CRLF 정규화 내용이 현재 main과 일치함을 대조했다.
- Linux 측정 보고서의 raw source SHA-256은
  `339007fb72cf9bd362bdcb9bf85ba1959532907901cda33c5136105bb0df5c6a`다.
  Source 지문이며 실제 배포 implementationDigest가 아니다. 각 profile의 ROOT·import 경로는
  같은 Linux checkout을 가리켰다.
- 준비 프로세스와 반복마다의 측정 프로세스를 분리했다. 실제 downloader의 파일 stream 대체,
  byte/hash·strict parse·adapt·Engine·projection·section hash·bounded JSON·AES-GCM을 사용했다.
  완료 profile의 pipeline은 read부터 암호화까지다. 실패 profile은 직렬화 거부까지 측정했다.
- 실험 보호값은 **RLIMIT_AS 2GiB / watchdog 60초 / 기본 출력 cap 64MiB**다.
  RLIMIT_AS는 virtual address space이고 RSS/cgroup 한도가 아니다. Production의 T/G/S/P·
  memory·동시성 설정이나 권장값으로 사용하지 않는다. 한 번에 한 profile만 측정했다.
- Linux 메모리는 `ru_maxrss`의 KiB를 bytes로 변환한 프로세스 전체 누적 peak다.
  Windows working set/commit과 측정 방식·Python patch·실행 환경이 다르므로 OS별 성능
  개선율을 계산하지 않는다. 시간은 3회 중앙값이며 p95/SLA 추정이 아니다.

## 2. 결과

MiB=1,048,576 bytes다. Dense는 2초 간격, spaced는 1시간 간격이며 evidence profile만
canonical record당 참조가 8개다. 모든 profile은 동일 generator의 합성 VersionSet을 쓴다.

| Profile | 입력 합계 MiB | 후보 수 | 결과 / cap | Pipeline 중앙값 초 | 최대 RSS MiB |
|---|---:|---:|---|---:|---:|
| dense32 | 0.085 | 256 | 0.265MiB 완료 | 0.080 | 73.8 |
| dense128 | 0.332 | 4,096 | 1.757MiB 완료 | 0.503 | 84.8 |
| dense256 | 0.661 | 16,384 | 5.420MiB 완료 | 1.795 | 111.1 |
| dense512 | 1.319 | 65,536 | 18.489MiB 완료 | 6.950 | 200.0 |
| spaced256 | 0.661 | 16,384 | 4.777MiB 완료 | 1.151 | 107.1 |
| evidence256x8 | 1.381 | 16,384 | 5.931MiB 완료 | 2.038 | 132.6 |
| cap256 | 0.661 | 16,384 | 1MiB cap 거부 | 1.551 | 102.1 |
| dense1024 | 2.637 | 262,144 | 64MiB cap 거부 | 34.992 | 447.7 |

앞 6개 profile은 각각 3회, 총 **18회 완료**했다. 각 profile의 전체 body SHA-256이 같았고,
Windows projection 비교의 동일 profile과 **inputHash·resultHash·전체 body SHA-256도 일치**했다.
고정 job/time과 합성 pin의 비교이며, 실제 OS별 deployment digest 간 hash 동일성을 뜻하지 않는다.

`cap256`과 `dense1024`는 각각 3회 모두 기존 `RESULT_PAYLOAD_TOO_LARGE`로 끝났다.
암호화·body 발행을 진행하지 않았으며 `determinism_passed=false`다. 성공 표본에 섞지 않았다.
총 표본은 24개이며 watchdog나 RLIMIT_AS에 도달한 사례는 없었다. 이 결과로 deadline·강제 중단·
OOM 분류가 검증됐다고 주장하지 않는다.

Dense512의 pipeline 범위는 6.819~7.155초, projection 중앙값은 0.621초였다.
Dense1024의 거부까지 시간은 33.994~35.160초였다. 첫 dense1024 표본은 read/parse 이후
약 83.2MiB, adapt 이후 86.2MiB, Engine 이후 207.9MiB, projection 이후 325.5MiB,
section hash 이후 447.6MiB까지 누적 peak가 늘었다. **직렬화 cap에 걸리기 전** 이미 큰 tree와
hash buffer를 보유했다. 최종 출력 cap만으로 메모리를 제한할 수 없다는 기존 관측을 Linux에서도 확인했다.

## 3. Linux 검증과 환경 실패 처리

- 실제 Linux 환경의 전체 pytest **689 passed / 131 skipped**. Windows용 IPv6 bootstrap 없이
  실행했다. Skip은 DB 환경 미구성과 기존 PoC 검사이며 통과로 세지 않는다.
- 측정 도구의 5개 검사도 포함했다. 실제 POSIX process group의 자손 종료, schema·Evidence·
  정상 metric·결정론·변조 거부·출력 cap을 검증했다. Production executor를 시험한 것은 아니다.
- Ruff check·formatter **260 files** 통과. Strict Pyright **0 errors / 0 warnings**.
- 최초 Pyright는 nodeenv가 `27.0.0-alpha.0.0.0`을 자동 선택한 뒤 버전 문자열 파싱에 실패해
  검사 자체가 시작되지 않았다. 소스 오류로 분류하지 않았다. 검증 전용
  `PYRIGHT_PYTHON_NODE_VERSION=22.16.0`을 지정해 같은 Pyright 1.1.411을 재실행했고 통과했다.
  최초 실패 로그를 남겼으며 runtime lockfile·소스·의존성을 변경하지 않았다.
- 초기 WSL 준비의 `/tmp` 폴더를 후속 호출에서 찾지 못한 실패도 보존했다. `/var/tmp` 전용
  폴더에서 준비·검사·계측·증적 수집을 한 세션으로 실행하고 결과를 Windows 측에 복사했다.
  첫 준비 성공을 테스트 통과로 해석하지 않았다.
- 현재 main과 측정 도구를 합친 Windows 조합도 재검증해 **689 passed / 131 skipped**와
  Ruff/formatter 260 files·strict Pyright 0 errors/0 warnings를 확인했다. Windows에만 기존
  외부 IPv6 bootstrap과 AnyIO assert-rewrite warning 1개가 있으며 Linux 결과와 구분한다.
  갱신한 PR 조합의 `verify-contract-sync.ps1 -IncludeWeb`도 통과했다.

재실행 명령과 Node 버전 고정 방법은 [도구 안내](../tools/README.md)를 따른다.
Raw artifact/report·실패/성공 로그·JUnit·환경 정보는 로컬 `refinvest-linux-analytics`에 보존하며
Git에 넣지 않는다. 단위 테스트와 로컬 가상환경만 사용했고 실제 Core·DB 호출은 실행하지 않았다.

## 4. 아직 남은 운영 gate

WSL은 cgroup v1/v2 혼합 구성이며 production Linux/systemd의 격리·종료·OOM 증거를 검증한
환경이 아니다. 현재 worker·미래 child/parent 각각의 peak, IPC/spool, 실제 network/trickle,
DB lock·publication 지연, 동시 worker의 합산 자원·종료·cleanup을 별도로 검증해야 한다.
이번 최대 입력은 합산 2.637MiB로, artifact별 허용 512MiB의 검증을 대체하지 않는다.

Core Analytics dispatch/publication/ACK runtime과 실제 E2E harness의 기존 공백도 남아 있다.
이 자료와 representative production 분포를 바탕으로 지원 입력·출력/관계 수·동시성 범위를
리뷰한 뒤 운영 수치안을 확정하고 ADR/계약 승인을 진행한다. 명시적 계약 결정 없이 후보를
잘라 성공시키거나 입력 cap을 조용히 낮추지 않는다. AR-08~10 체크박스는 계속 미완료다.

Determinism impact: none. Point-in-Time impact: none.
