# Pi OJ

Pi OJ is a lightweight online judge workspace for Raspberry Pi 5. It provides a browser editor, local sandboxed runs, UVA integration, Codeforces problem/sample loading, and AI helper tools.

## Features

- Local run with Docker sandbox
- UVA problem PDF loading and sample extraction
- Codeforces problem page loading and sample extraction
- AI hint/debug/translation/test-data helpers
- Wrong Answer diff table for local tests
- ngrok-friendly private access
- Judge-specific language lists
- CPE star problem picker grouped by level
- Independent CPE exam page with strict / standard / practice / custom modes
- CPE exam history with problem-id and submitted-problem summaries
- Monaco editor with simple compiler fallback when editor assets fail or timeout

## Quick Start

```bash
cd ~/pi_oj
cp .env.example .env
mkdir -p backend/sandboxes
docker compose build judge-image web worker
docker compose up -d redis web worker
```

Open:

```text
http://<raspberry-pi-ip>:8000
```

For ngrok, keep the app bound to `127.0.0.1:8000` and run:

```bash
chmod +x start-ngrok.sh
./start-ngrok.sh
```

The script uses this reserved URL by default:

```text
https://unremovable-unrepeatable-phuong.ngrok-free.dev
```

If `ngrok-policy.yaml` exists, the script also applies it automatically. If it does not exist, the script prints a warning before starting ngrok without policy protection.

## Maintenance Scripts

```bash
# Check local Monaco / Vim editor assets
sh scripts/check-editor-assets.sh

# Check editor assets through a running web service
WEB_URL=http://127.0.0.1:8000 sh scripts/check-editor-assets.sh

# Check web health and ngrok tunnel status
sh scripts/check-ngrok.sh

# Backup project config and CPE star list
sh scripts/backup-data.sh

# Optional Playwright smoke test
BASE_URL=http://127.0.0.1:8000 node scripts/e2e-smoke.js
```

The backup script saves server-side project settings. CPE exam history lives in browser localStorage, so export it from the exam UI when you want to archive exam records.

### E2E Smoke Test

`scripts/e2e-smoke.js` is a small Playwright check for the main page and the CPE exam page. It opens the site in a real browser, switches the test/AI panels, checks that the editor area exists, and fails if important static resources return 4xx/5xx.

This repository does not require committing `node_modules/`. Install Playwright only on the machine where you want to run the smoke test:

```bash
cd ~/pi_oj
npm i -D playwright
npx playwright install chromium
```

If Playwright says the OS is missing browser dependencies, install them once:

```bash
sudo npx playwright install-deps
```

Then start Pi OJ and run the smoke test:

```bash
cd ~/pi_oj
docker compose up -d web worker
BASE_URL=http://127.0.0.1:8000 node scripts/e2e-smoke.js
```

Expected success output:

```text
E2E smoke test passed.
```

For ngrok or another host, change `BASE_URL`:

```bash
BASE_URL=https://your-ngrok-url node scripts/e2e-smoke.js
```

Do not commit `node_modules/`, `package.json`, or `package-lock.json`; they are ignored because Playwright is currently only a local smoke-test dependency. If you decide to make E2E a formal npm-managed workflow later, remove `package.json` and `package-lock.json` from `.gitignore` and commit them, but still keep `node_modules/` out of git.

## Environment

`.env` must be placed at the project root, for example `~/pi_oj/.env`.

Important values:

```dotenv
HOST_SANDBOX_ROOT=/home/ian/pi_oj/backend/sandboxes
UVA_USERNAME=
UVA_PASSWORD=
UVA_LOGIN_MODE=browser
AI_PROVIDER=gemini
GEMINI_API_KEY=
OJ_STDOUT_CAPTURE_LIMIT=120000
OJ_STDERR_CAPTURE_LIMIT=40000
OJ_JAVA_MEM_LIMIT=384m
OJ_GO_MEM_LIMIT=256m
```

Codeforces automatic server-side submit is intentionally not relied on because Cloudflare blocks headless worker automation. The UI opens the submit page and copies code for manual submission.

## Language Groups

### Enabled Local Runners

These languages are installed in `pi_oj-judge` and can be used for local tests:

- C
- C++11 / C++17 / C++20 / C++23
- Python 3
- PyPy 3
- Java
- Node.js
- JavaScript V8 shim via Node.js `readline()` compatibility
- Pascal / Free Pascal
- Go
- Rust 2021 / Rust 2024
- Perl
- PHP
- Ruby
- OCaml
- D via LDC

### UVA Language List

When judge is `UVA`, the language selector shows:

- ANSI C 5.3.0
- JAVA 1.8.0
- PASCAL 3.0.0
- C++11 5.3.0
- PYTH3 3.5.1

Submit language ids are configurable:

```dotenv
UVA_C_LANGUAGE_ID=1
UVA_JAVA_LANGUAGE_ID=2
UVA_PASCAL_LANGUAGE_ID=3
UVA_CPP_LANGUAGE_ID=5
UVA_PY_LANGUAGE_ID=6
```

### Codeforces Language List

When judge is `Codeforces`, the language selector shows practical Pi-friendly languages:

- GNU GCC C11 5.1.0
- GNU G++17 7.3.0
- GNU G++20 13.2
- GNU G++23 14.2
- Java 21 64bit
- Python 3.13.2
- PyPy 3.10
- JavaScript V8
- Node.js 15.8.0
- Rust 1.89.0 (2021)
- Rust 1.89.0 (2024)
- Go 1.22.2
- Perl 5.20.1
- PHP 8.1.7
- Ruby 3.2.2
- OCaml 4.02.1
- D DMD32 2.105.0

Codeforces language ids are configurable in `.env`. They are mostly used if a future browser/helper submit path is added.

## Not Enabled Because They Are Too Heavy Or Awkward On Pi 5

These are documented for future extension, but not shown in the default UI and not installed in the judge image:

- C#
- F#
- Scala
- Kotlin
- Delphi
- PascalABC.NET

Suggested extension design:

- Keep the default `Dockerfile.judge` focused on the enabled local runners.
- Add a separate `Dockerfile.judge-advanced` for heavy runtimes.
- Add a `JUDGE_IMAGE_ADVANCED` option and route selected advanced languages to that image.
- Show advanced languages only when the advanced image is enabled.

## Rebuild After Language Changes

Because language runtimes live in the judge image, rebuild it after changing `Dockerfile.judge`:

```bash
docker compose build judge-image web worker
docker compose up -d --force-recreate web worker
```

## CPE Star Problems

The CPE picker is grouped by required / 1-star / 2-star / 3-star / 4-star / 5-star lists.

Problem references are based on YuiHuang's CPE collection:

- https://yuihuang.com/cpe/

The original CPE environment page can still be used as the authority when the list changes.

## CPE Exam Mode

The independent CPE exam page is implemented at `/static/cpe-exam.html` and can be opened from the main page.

### Exam Page

- Use a separate exam page instead of adding the exam flow to the practice page.
- Enter the exam from the main page by opening a separate browser window, not a normal tab.
- Use a best-effort locked exam window:
  - Disable the context menu.
  - Block common refresh shortcuts such as F5 and Ctrl+R.
  - Block common developer-tool shortcuts such as F12, Ctrl+Shift+I, Ctrl+Shift+J, and Ctrl+U.
  - Avoid normal practice-page navigation controls inside the exam window.
- Browser-side blocking is only a deterrent, not a real security boundary; serious enforcement still requires server-side logging and proctoring rules.
- Use CPE star problems, but submit through the UVA path.
- Show the exam title, remaining time, problem list, current problem, submit result, and last answer summary.
- History records show the drawn problem ids and the submitted problem ids so users can quickly review which UVA problems they practiced.
- Test-source buffers are separated by source. Switching among sample, uDebug, custom, and AI tests loads that source's own input/output instead of carrying over the previous source's text.
- Refreshing the page must not redraw problems or lose code.

### Exam Rules

- Time limit: 3 hours.
- Total problems: 7.
- Problem distribution:
  - 1 required problem from Must 49.
  - 2 problems from 1-star.
  - 2 problems from 2-star.
  - 1 problem from 3-star.
  - 1 problem from 4-star.
- Allow redraw before the exam starts.
- Freeze the problem list after the exam starts.
- Show a simple summary after the exam ends.

### Tool Modes

- Strict mode:
  - Disable AI helper.
  - Disable AI test data.
  - Disable uDebug.
  - Disable Vim.
  - Use exam compiler.
- Standard mode:
  - Disable AI helper.
  - Allow AI test data.
  - Allow uDebug.
  - Disable Vim.
  - Use exam compiler.
- Practice mode:
  - Allow AI helper.
  - Allow AI test data.
  - Allow uDebug.
  - Allow Vim toggle.
  - Allow OJ compiler, exam compiler, or simple compiler.
- Custom mode:
  - Allow per-tool settings for AI helper, AI test data, uDebug, Vim, and compiler type.

### Permission Groups

- AI helper:
  - Custom prompt.
  - Hint.
  - Debug.
  - Translation.
- Test tools:
  - uDebug.
  - AI test data.
  - AI expected output for custom input.
- Editor tools:
  - Vim.
- Compiler:
  - OJ compiler.
  - Exam compiler.
  - Simple compiler, which uses the textarea editor but still runs through the same backend judge path.

### Saved State

- Save the problem list in localStorage.
- Save the start time.
- Save code independently for every problem.
- Save the last result for every problem.
- Save the submit count for every problem.
- Save the last answer time for every problem.
- Save the language for every problem.
- Save the exam mode and permissions.

### Last Answer Summary

- Show problem id.
- Show language.
- Show last submit time.
- Show last result, such as AC / WA / CE / TLE.
- Show submit count.
- If only a local test exists, show the local test result.
- If a UVA submit exists, prefer the UVA result.

### Final Summary And Export

- Show AC count.
- Show WA / CE / TLE counts.
- Show each problem's final status.
- Show each problem's submit count.
- Show total used time.
- Allow exporting the exam record.

### Exam Compiler Choices

- C (MinGW GCC 6.3.0)
- C++ (MinGW GCC 6.3.0)
- Java (AdoptOpenJDK 8)
- Java (AdoptOpenJDK 11)
- Python (Python 3.9.6)

## Local Run Limits

Local judging captures stdout and stderr through an in-container wrapper instead of Docker logs, so runaway output is truncated before it can fill Docker log storage. Defaults:

- `OJ_STDOUT_CAPTURE_LIMIT=120000`
- `OJ_STDERR_CAPTURE_LIMIT=40000`
- `OJ_JAVA_MEM_LIMIT=384m`
- `OJ_GO_MEM_LIMIT=256m`

If stdout or stderr exceeds its capture limit, the local verdict becomes `OLE` and the returned output/error log is truncated. Java compile/run containers use the Java memory limit above and run Java with `-Xmx256m` by default. Go local builds set `GOCACHE` / `GOMODCACHE` under `/tmp` to avoid read-only-home cache failures.

## AI Helper

Supported AI features:

- Hint
- Debug
- Problem translation
- AI-generated local tests
- AI expected output for custom input

The frontend sends only the task context to the backend; API keys stay in the worker container.

## Security Notes

- The web container does not mount Docker socket.
- Only the worker mounts Docker socket so it can launch judge containers.
- Do not expose Redis or Celery to public networks.
- The worker has high host privileges through Docker socket access; keep ngrok/auth policies strict.
- Use `ngrok-policy.yaml` or another access layer when exposing the app outside your private network.
- Local runs use per-submission directories under `backend/sandboxes` and remove them after execution.

## Private-instance access protection

Pi OJ now requires HTTP Basic authentication for all pages and API endpoints except `/api/health`.
When `patch_pi_oj.py --apply` runs, it adds a random password to the ignored `.env` file.
See `.env` for `PI_OJ_BASIC_USER` and `PI_OJ_BASIC_PASSWORD`; **never commit `.env`**.
Use an HTTPS ngrok URL for remote use, not plaintext HTTP on a LAN.

The web port now binds to localhost by default: `127.0.0.1:8000:8000`.
If you intentionally need LAN access, modify `PI_OJ_BIND_HOST`, but understand that Basic Auth
over plaintext HTTP exposes credentials. Keep firewall rules and ngrok access policies enabled.

The API has conservative **in-memory** per-route request quotas. These protect a
single-owner/single-Web-worker deployment, not public multi-user hosting. Restart
resets quotas. Use Redis-backed limits for multiple Web workers.

**Important residual risks:** the Worker still controls the Docker socket, and
the writable host bind mount `/sandbox` still has no enforceable total disk quota.
Do not invite arbitrary untrusted public users until a separate sandbox runtime
and dedicated limited-size host sandbox filesystem are configured and verified.

## Queue admission and stdin safety

The web API now rejects newly requested Celery tasks when the Redis `celery`
waiting queue reaches `PI_OJ_MAX_QUEUE_DEPTH` (default 20). This is **best-effort**
back-pressure, not an atomic global quota: Celery reserves jobs, and concurrent
HTTP requests can still briefly exceed the limit. Existing per-route rate limits
remain in effect.

Local judge input is written into the per-submission `.stdin` file and read from
there by the in-container runner. This avoids a blocked Docker socket write when
a submitted program does not read its input. Input size remains restricted by
FastAPI's request model.

### Storage protection is host-level and must be enabled separately

This patch copies `scripts/setup_sandbox_tmpfs.sh`; it **does not mount any
filesystem automatically**. On the Raspberry Pi host, after checking no judging
jobs are running and verifying `.env` `HOST_SANDBOX_ROOT` is the actual Pi path,
run `sudo bash scripts/setup_sandbox_tmpfs.sh 512M`. This caps *all* sandbox
submissions together to 512 MiB of RAM-backed filesystem. It is lost on reboot
and is not a per-job disk quota. Never mount it blindly on the WSL `/mnt/c/`
checkout or while a judge is running. Configure persistence separately.

**Not solved:** `worker` still runs with host-powerful Docker Socket access.
Neither basic authentication nor this patch turns Docker into a hardened
public multi-tenant execution system. Use a dedicated isolated judge host or
VM and review its network boundaries before open public access.
