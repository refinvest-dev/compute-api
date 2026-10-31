# Analytics resource baseline

AR-08의 운영 수치 검토를 위한 개발용 도구다. 서비스·worker·API·DB에 연결하지 않는다.
실행 deadline, OS 메모리 제한이나 정책 승인을 구현하지 않는다.

저장소 루트에서 개발 가상환경과 현재 checkout의 `src`를 사용한다. 결과는 저장소 밖의
새 디렉터리에 기록한다. 이미 있는 디렉터리는 덮어쓰지 않는다.

```powershell
$env:PYTHONPATH = "$PWD/src;$PWD"
.venv/Scripts/python.exe -m tools.analytics_resource_baseline run "$env:TEMP/analytics-dense-128" --episodes 128 --spacing-seconds 2 --repetitions 3
```

별도 worktree라면 가상환경 Python의 절대 경로를 사용하고 `PYTHONPATH`는 해당 worktree를
가리키게 한다. Linux에서는 Python 경로와 `PYTHONPATH` 구분자를 맞춘다.

Linux의 새 검증 폴더에서 같은 lockfile과 checkout을 사용한다. 다음 보호값은 실험용이다.

```sh
uv sync --locked --dev --python 3.11
export PYTHONPATH="$PWD/src:$PWD"
run_root="$(mktemp -d /var/tmp/refinvest-analytics.XXXXXX)"
(
  ulimit -v 2097152
  .venv/bin/python -m tools.analytics_resource_baseline run "$run_root/dense512" --episodes 512 --repetitions 3
)
PYRIGHT_PYTHON_NODE_VERSION=22.16.0 PYRIGHT_PYTHON_CACHE_DIR="$run_root/pyright-cache" \
  .venv/bin/pyright --pythonpath "$PWD/.venv/bin/python"
```

`ulimit -v`는 Linux virtual address space를 2GiB로 제한하며 RSS/cgroup 제한은 아니다.
Node 버전은 pyright wrapper의 자동 alpha 버전 선택·파싱 실패를 피하기 위한 검사 환경 설정이다.
Runtime 정책·의존성이나 잠금파일을 바꾸지 않는다. 실제 측정 환경·결과·실패와 한계는
[Linux 실측 기록](../docs/ANALYTICS_RESOURCE_LINUX.md)에 있다. WSL에서는 세션 간 검증 폴더 보존을
확인하고, 종료 전 raw report/log를 별도 로컬 증적 폴더로 복사한다.

- `--spacing-seconds 2`: 한 instrument의 중첩 없는 빠른 거래, 30분 window 내 후보·baseline.
- `--spacing-seconds 3600`: window 밖 후보. 현재 엔진은 `AFTER_WINDOW`도 결과에 포함한다.
- `--evidence-per-record 8`: canonical record당 서로 다른 합성 source 참조 8개.
- 기본 출력 cap 64MiB, watchdog 60초, 반복 3회는 **측정 도구의 보호값**이며 운영 권장값이 아니다.
  Watchdog는 Windows의 `taskkill /T` 또는 POSIX의 전용 process group으로 이 도구가 생성한
  프로세스들을 정리하고 wait한다. Windows venv redirector의 하위 Python 생존을 실제 테스트한다.
  메모리 hard limit, production executor의 crash/IPC/OS 감독을 보장하지 않는다.
  큰 profile은 격리된 호스트에서 실행한다.

`prepare`는 변경하지 않은 synthetic fixed vector의 형식·config·opaque upstream pin을 기반으로
고유한 record/unit/allocation/evidence ID와 OPEN/CLOSE 쌍을 만든다. 손실·이익이 번갈아 나오며
ledger hash, 개별 synthetic record hash, manifest hash·counts, artifact bytes/hash, config/input
identity를 다시 만든다. 원본 golden fixture는 변경하지 않는다. **실제 Normalization 산출물이나
production VersionSet/digest가 아니다.** upstream inputSet/execution/quality/terms pin은 vector의
opaque 합성값을 유지한다. Normalization→Core 생성 경로의 정합성 증거로 사용하지 않는다.

`run`은 준비 프로세스를 종료한 다음 각 반복마다 새로운 Python 프로세스를 실행한다.
따라서 generator의 객체·메모리 peak가 측정 자식에 섞이지 않는다. 측정 자식에서는 HTTP opener만
파일 stream으로 바꾸고 실제 downloader의 chunk/size/hash 검사·strict parser, 입력 검증·adapter,
Engine, projection, section hash·완전한 payload 직렬화, body hash와 실제 AES-GCM 함수를 호출한다.
임시 암호화 key를 저장하지 않는다. Ciphertext는 nonce 때문에 매번 달라지며 결정론 비교 대상은
고정 job/time을 사용한 평문 body hash다. 암호화 후 DB transaction, descriptor 구성과 Core 전달은 제외한다.

`report.json`은 단계별 시간·프로세스 OS high-water mark, 전체 pipeline 시간, fresh process의
생성부터 종료까지의 시간, 입력/출력 크기, 후보 수·metric 상태·section counts, source SHA-256,
플랫폼·Python·CPU를 기록한다. Pipeline 시간은 import 후 read부터 암호화까지다. Windows는
`GetProcessMemoryInfo`의 peak working set과 peak commit을 사용한다. Linux/macOS는 `ru_maxrss`만
보고한다. 단계별 peak는 **프로세스 시작 이후 누적 최대치**이며 단계별 증가량으로 더하지 않는다.
실제 HTTP 지연·trickle, DB lock, heartbeat, child IPC/spool, 다중 worker의 동시 peak는 측정하지 않는다.
Watchdog 종료는 실패한 표본으로 남기며 완료·결정론 통과로 계산하지 않는다.

Raw 합성 입력·report는 로컬 검증 산출물로 보존하고 Git에 커밋하지 않는다. 저장소에는 도구,
정합성/결정론/변조/출력 cap 테스트와 해석 가능한 요약 문서를 둔다. OS·컨테이너·Python·의존성·
Engine·projection·상한·동시성이 바뀌면 해당 profile을 다시 측정한다. 운영 수치 확정에는 지원 Linux
배포 환경, 최대 허용 입력·출력, 느린 전송·DB 경합·실제 Core E2E 검증이 추가로 필요하다.

## 실제 HTTP 입력 실측

`analytics_http_baseline`은 별도 프로세스의 IPv4 loopback HTTP 서버로 고정 16 episode의 합성
Ledger/Manifest를 전송한다. 실제 downloader·크기/hash 검사·strict JSON parser·입력 adapter를
사용하며 HTTP opener를 대체하지 않는다. 서버·generator와 반복별 client 프로세스를 분리해
client의 누적 peak RSS를 측정한다. 외부 Core·DB·TLS·Engine·projection·payload 발행은 제외한다.

```sh
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m tools.analytics_http_baseline run \
  /var/tmp/analytics-http-small --artifact-bytes 1048576 --repetitions 3
```

`--artifact-bytes`는 **각 artifact의 크기**다. 양쪽 JSON 뒤에 유효한 whitespace를 stream으로
추가하고 byteSize·SHA-256·input identity를 다시 계산한다. JSON tree·record 수가 큰 입력을
대체하지 않으며 서로 다른 크기의 inputHash를 같다고 주장하지 않는다. 같은 크기·전송 조건의
반복에서 inputHash·논리 document hash를 비교한다. Golden fixture·production pin은 바꾸지 않는다.

`--send-chunk-bytes`, `--chunk-delay-seconds`로 trickle을, `--grant-seconds`로 짧은 만료를
시험할 수 있다. `--max-input-bytes`는 실험 client에 전달하는 기존 downloader 상한이다.
0초 grant·상한 거부·hash 오류는 실패 표본으로 남는다. `INPUTS_VERIFIED`와 `BENCHMARK_*`는
도구의 상태 이름이며 API나 production 오류 code를 추가하지 않는다. Watchdog·process failure·
MemoryError는 성공이나 결정론 통과로 계산하지 않는다.

512MiB 상한·60초 watchdog 등의 CLI 범위는 실험 보호값이다. 큰 입력은 주소 공간 제한을 둔
격리 Linux 프로세스에서 순차 실행하고, RLIMIT_AS와 RSS/cgroup 한도를 구분한다. 운영 T/G/S/P·
hard limit·동시성 승인이나 실제 Core Analytics E2E의 증거로 사용하지 않는다.

실행 profile·검증 결과와 한계는 [HTTP 실측 기록](../docs/ANALYTICS_HTTP_RESOURCE_BASELINE.md)에 정리한다.
