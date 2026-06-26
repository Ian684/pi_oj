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
ngrok http 8000 --traffic-policy-file ngrok-policy.yaml
```

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
- Local runs use per-submission directories under `backend/sandboxes` and remove them after execution.

