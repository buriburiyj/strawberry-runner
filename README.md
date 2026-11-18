# 🍓 strawberry-runner

**공개 GitHub 레포의 Actions를 무료 실행기로 쓰고, 결과는 `strawberry-server`(D1)에만 저장하는 러너.**
공개 레포는 Actions 분(minute)이 무제한 무료라서, 크롤링·웹 체크·빌드처럼 **공개돼도 상관없는 작업**을 돌리는 용도다.

- 실행: `workflow_dispatch`(수동 / `gh workflow run` / API) **하나만** 허용
- 결과: 태스크의 stdout·stderr·exit code를 `PUT /kv/runner:<task_id>` 로 저장 + 요약 한 줄을 `POST /logs`
- 로그: GitHub Actions 화면에는 **`task_id`, `mode`, `exit_code` 세 줄만** 찍힘

```
strawberry-runner/
├── .github/workflows/run-task.yml   # 트리거·권한·env 정의
├── scripts/execute.py               # 실제 실행 + 결과 전송 (표준 라이브러리만)
├── tests/test_execute.py            # 모의 서버로 돌리는 로컬 E2E 테스트 84개
└── README.md
```

---

## 1. 왜 공개 레포인데 안전한가

공개 레포에서는 **누구나 Actions 로그를 읽을 수 있다**는 전제로 설계했다.

| 위험 | 막는 방법 |
| --- | --- |
| 남이 마음대로 실행 | 트리거가 `workflow_dispatch` 뿐. `pull_request`·`issues`·`push`·`schedule` 는 없음 → 레포 write 권한이 있는 사람만 실행 가능 |
| 로그에 개인정보·페이로드 노출 | `scripts/execute.py` 는 `task_id`·`mode`·`exit_code` 외에는 **stdout/stderr에 아무것도 쓰지 않음**. 태스크 출력과 응답 본문은 HTTPS로 서버에 바로 저장되고 로그로 가지 않음 |
| 토큰 노출 | 토큰은 GitHub Secrets에만. 작업 프로세스에 넘기는 환경변수에서 `STRAWBERRY_TOKEN`·`STRAWBERRY_URL`·`PAYLOAD_B64` 를 **제거** — 태스크가 `echo $STRAWBERRY_TOKEN` 을 해도 빈 값이 나옴 |
| 레포 변조·코드 실행 권한 남용 | `permissions: contents: read` (쓰기 권한 없음) |
| 오류 메시지로 대상 URL 유출 | urllib 예외 메시지를 버리고 예외 클래스명/HTTP 상태코드만 남김 |

**정직하게 알아둘 한계 두 가지**

1. `payload_b64` 는 **암호화가 아니다.** base64는 그냥 인코딩이라 누구나 풀 수 있다. 게다가 `workflow_dispatch` 입력값은 공개 레포 런의 "Run details"·이벤트 API에서 조회된다. → **페이로드에 비밀번호·API 키·개인정보를 넣지 말 것.** 로그 노출을 막아줄 뿐, 입력 자체를 숨겨주지는 않는다.
2. 정말 민감한 작업(개인 메모 처리, 사적 데이터 크롤링)은 이 러너가 아니라 **비공개 레포 러너**(월 2,000분 무료)로 돌린다.

> 태스크 안에서는 `strawberry-server` 를 호출할 수 없다(토큰을 일부러 안 넘긴다). 러너가 결과를 저장하는 건 러너 프로세스 자신이 한다. 태스크가 서버에 뭔가를 남겨야 한다면 페이로드에 토큰을 넣지 말고, 결과 JSON을 돌려받아 밖에서 처리한다.

---

## 2. 준비 (한 번만)

### ① 공개 레포 만들기

https://github.com/new → Repository name **`strawberry-runner`** → **Public** → README/라이선스/gitignore **모두 체크 해제** → Create repository

그다음 이 폴더를 그대로 올린다 (이미 커밋이 만들어져 있으면 그 커밋을 push):

```bash
cd strawberry-runner
git remote add origin https://github.com/buriburiyj/strawberry-runner.git
git push -u origin main
```

### ② Secrets 2개 넣기

레포 → Settings → Secrets and variables → Actions → **New repository secret**

| 이름 | 값 |
| --- | --- |
| `STRAWBERRY_URL` | `https://strawberry-server.buriburiyejun.workers.dev` |
| `STRAWBERRY_TOKEN` | `strawberry-server` 의 `API_TOKEN` (배포할 때 `wrangler secret put API_TOKEN` 에 넣은 그 64자리 값, 로컬 `.dev.vars` 의 `API_TOKEN=` 뒤에 있음) |

CLI로 넣으려면 (토큰은 붙여넣기 창에 입력, 셸 기록에 안 남음):

```bash
gh secret set STRAWBERRY_URL   --repo buriburiyj/strawberry-runner --body "https://strawberry-server.buriburiyejun.workers.dev"
gh secret set STRAWBERRY_TOKEN --repo buriburiyj/strawberry-runner
```

> 토큰을 바꾸면(`wrangler secret put API_TOKEN`) 여기 Secret도 같이 바꿔야 한다.

---

## 3. 실행

페이로드는 **base64로 인코딩해서** 넘긴다. 줄바꿈·따옴표·한글이 셸과 YAML을 깨뜨리지 않게 하기 위함이다.

```bash
# 0) 자주 쓰는 값
REPO=buriburiyj/strawberry-runner
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
```

### a) 웹 체크 (`http_check`)

```bash
gh workflow run run-task.yml -R $REPO \
  -f task_id=health-1 -f mode=http_check \
  -f payload_b64="$(b64 'https://strawberry-server.buriburiyejun.workers.dev/health')"
```

JSON으로 세밀하게: `{"url": "...", "method": "POST", "headers": {"X-A": "1"}, "data": {"k": "v"}}`
→ 상태코드 400 이상이면 `exit_code=1` 로 기록된다(2xx/3xx = 0). 타임아웃 20초.

### b) 파이썬 스크립트 (`python_script`)

```bash
gh workflow run run-task.yml -R $REPO \
  -f task_id=py-1 -f mode=python_script \
  -f payload_b64="$(b64 'import platform, sys
print(sys.version)
print(platform.platform())')"
```

임시 폴더에서 `python3 task.py` 로 실행(파이썬 3.12), 제한 120초. 로컬 파일은 없다 — 필요한 파일은 스크립트가 직접 받아야 한다.

### c) 셸 명령 (`shell_cmd`)

```bash
gh workflow run run-task.yml -R $REPO \
  -f task_id=sh-1 -f mode=shell_cmd \
  -f payload_b64="$(b64 'ls -la && python3 -V')"
```

`bash -c` 로 실행, 제한 120초.

### 결과 확인 (GitHub 로그 말고 여기서)

```bash
BASE=https://strawberry-server.buriburiyejun.workers.dev
TOKEN=<API_TOKEN>

curl -s -H "Authorization: Bearer $TOKEN" "$BASE/kv/runner:health-1"          # 결과 전체
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/logs?source=github-runner&limit=5"  # 요약 로그
```

---

## 4. 저장되는 결과 모양

`PUT /kv/runner:<task_id>` body `{"value": <result>}`:

```json
{
  "task_id": "health-1",
  "mode": "http_check",
  "exit_code": 0,
  "stdout": "{\"ok\":true,...}",      // 최대 16KB
  "stderr": "",                        // 최대 8KB
  "duration_ms": 412,
  "finished_at": "2026-11-18T07:42:10.315Z",
  "http_status": 200,
  "truncated": {"stdout": false, "stderr": false},
  "timed_out": false
}
```

`POST /logs`:

```json
{
  "source": "github-runner",
  "event": "info",                     // "info" | "error"  ← 서버가 요구하는 필수 필드
  "level": "info",
  "message": "task health-1 (http_check) exit=0",
  "meta": {"task_id": "health-1", "mode": "http_check", "exit_code": 0},
  "payload": {"level": "info", "message": "...", "task_id": "...", "mode": "...", "exit_code": 0,
              "duration_ms": 412, "timed_out": false, "http_status": 200}
}
```

> `strawberry-server` 의 `/logs` 는 `source` 와 `event` 를 **필수**로 받는다. `{level, message, meta}` 만 보내면 `400 invalid_field: event is required.` 가 떨어진다. 그래서 `event` 에 레벨 값을 넣고, 계획서의 `level`·`message`·`meta` 도 그대로 함께 보낸다.

---

## 5. 종료 코드

| `execute.py` 종료 | 뜻 | GitHub 실행 결과 |
| --- | --- | --- |
| 0 | 태스크 성공 + 결과 저장 완료 | ✅ |
| 1 | 태스크가 실패(exit≠0)했지만 결과는 저장됨 | ❌ (기록은 남음) |
| 2 | 입력/설정 오류 — 아무것도 실행·전송 안 함 | ❌ |
| 3 | 결과 저장 실패(401/네트워크 등) | ❌ |
| 4 | 내부 오류 | ❌ |

태스크 자체가 124로 끝나면 `timed_out: true`(제한 시간 초과)가 같이 기록된다.

## 6. 한도 (러너가 강제)

| 항목 | 값 |
| --- | --- |
| 페이로드 | 64KB (base64 약 87KB — 환경변수 128KB 한계 안전선) |
| `http_check` 타임아웃 | 20초 (`RUNNER_HTTP_TIMEOUT`) |
| `python_script` / `shell_cmd` | 120초 (`RUNNER_TASK_TIMEOUT`) |
| stdout / stderr 저장 | 16KB / 8KB 까지 (넘으면 잘리고 `truncated: true`) |
| 잡 전체 | 10분 (`timeout-minutes`) |
| 동시 실행 | `task_id` 별로 직렬화(같은 id 중복 실행 방지) |

## 7. 로컬에서 먼저 검증하기

```bash
python3 -m py_compile scripts/execute.py          # 문법 검사
python3 tests/test_execute.py                     # 모의 서버로 84개 체크 (네트워크 불필요)

# 서버 없이 태스크만 돌려보기 (페이로드·본문은 출력하지 않고 길이만 보여줌)
TASK_ID=dry-1 MODE=shell_cmd PAYLOAD_B64="$(printf '%s' 'echo hi' | base64 | tr -d '\n')" \
  python3 scripts/execute.py --dry-run
```

테스트가 확인하는 것: 세 모드 정상 동작, 4xx/타임아웃/네트워크 오류, 출력 절단(16KB/8KB),
잘못된 base64·task_id·mode·64KB 초과 페이로드 거부, 401/서버 다운 시 종료 코드 3,
**페이로드·본문·토큰이 stdout/stderr에 절대 안 나오는지**, 워크플로 파일의 트리거·권한·env 구성.

## 8. 무료 AI·0원 혜택 수집기 (`scripts/collect_deals.py`)

이 러너의 첫 번째 상시 작업. 공개 소스를 **실제로 열어 파싱한 값만** 모아 JSON 한 덩어리로
stdout에 찍는다. 준비된 소스 13곳(2026-11-18 실측):

| 구역 | 무엇을 | 소스 |
| --- | --- | --- |
| `catalogs` | 무료 모델 개수·ID를 가격표 필드로 직접 집계 | OpenRouter · NVIDIA NIM · Kilo · OVHcloud · Pollinations |
| `offers` | 공식 페이지의 무료 조건 문장을 그대로 발췌 | Oracle Cloud · Google Cloud · Cloudflare Workers · GitHub Copilot · AWS Kiro |
| `free_games` | 지금·예정 0원 배포 | Epic Games(KR) · Steam 100% 할인(KR) |
| `curated` | `buriburiyj.github.io/free-deals` 기존 혜택 22건 생존·마감 확인 | 사이트의 data.json |

```bash
python3 scripts/collect_deals.py                 # stdout에 JSON 1개(약 11KB, 1~5초)
python3 scripts/collect_deals.py --pretty --out deals.json
python3 scripts/collect_deals.py --only catalogs,free_games
python3 tests/test_collect_deals.py              # 파서 단위 테스트 17개(네트워크 불필요)
```

러너에서 돌릴 때는 페이로드를 작게 유지하려고 저장소의 스크립트를 직접 받아 실행한다:

```bash
gh workflow run run-task.yml -R buriburiyj/strawberry-runner \
  -f task_id=deals-$(date -u +%Y%m%dT%H%MZ) -f mode=shell_cmd \
  -f payload_b64="$(printf '%s' 'curl -fsSL https://raw.githubusercontent.com/buriburiyj/strawberry-runner/main/scripts/collect_deals.py -o /tmp/c.py && python3 /tmp/c.py' | base64 | tr -d '\n')"
```

결과는 `runner:<task_id>` 의 `stdout` 에 JSON 문자열로 저장된다. 그 JSON을 D1에 누적
(`deals:latest`, `deals:run:<run_id>`, `POST /notes`)하고 사이트(`free-deals/live.json`)에
반영하는 일은 이 러너가 아니라 **예약 작업**이 한다(토큰을 러너 태스크에 넘기지 않는 설계
때문). 그 예약 작업 정의는 Pickle 쪽 `scheduled-actions/` 에 있다.

### 이 스크립트가 지키는 선

- 토큰·API 키를 쓰지 않는다. 비밀값이 필요 없는 공개 페이지만 본다.
- 추측하지 않는다. 파싱 실패는 `sources[].error`, 문구 없음은 `note` 로 남고 값은 `null`/빈 배열.
- stdout 15KB 상한. 넘으면 목록 개수와 발췌 길이를 줄이고, 그래도 넘으면 상세만 비운다.
- 소스 한 곳이 죽어도 나머지는 계속 간다(스레드 8개, 소스별 타임아웃 20초).

## 9. 문제 해결

| 증상 | 원인 / 해결 |
| --- | --- |
| 실행이 안 됨(트리거 없음) | `workflow_dispatch` 는 레포 write 권한 필요 → 본인 계정으로 로그인했는지 확인. 포크에서는 안 됨 |
| `delivery_failed: HTTP 401` | `STRAWBERRY_TOKEN` Secret이 서버 토큰과 다름 → `wrangler secret put API_TOKEN` 값으로 다시 넣기 |
| `delivery_failed: HTTP 404` | `STRAWBERRY_URL` 오타 (끝에 `/` 없이) |
| `error: TASK_ID must be ...` | `task_id` 에 공백·`/`·한글 사용 → `A-Z a-z 0-9 . _ : -` 만, 120자 이하 |
| `error: payload is larger than 64KB` | 큰 작업은 스크립트를 URL로 받아 실행하도록 바꾸기 |
| 결과가 안 보임 | `GET /kv?prefix=runner:` 로 키 확인, `GET /logs?source=github-runner` 로 요약 확인 |
| Actions 분이 걱정됨 | **공개 레포는 Actions 분 무제한 무료**(비공개는 월 2,000분) |
