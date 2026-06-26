import os
import re
import shutil
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import docker
import requests
from bs4 import BeautifulSoup
from celery import Celery
from docker.errors import ContainerError, ImageNotFound

worker = Celery(
    "oj_tasks",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0"),
)
worker.conf.update(result_expires=3600, task_track_started=True)
# Celery CLI 預設會在 tasks 模組裡找 app/celery 這類常見名稱。
# 保留 worker 給既有程式 import，同時提供 app 讓 `celery -A tasks worker` 穩定啟動。
app = worker

SANDBOX_ROOT = Path("/sandboxes")
HOST_SANDBOX_ROOT = Path(os.getenv("HOST_SANDBOX_ROOT", "/home/ian/pi_oj/backend/sandboxes"))
JUDGE_IMAGE = os.getenv("JUDGE_IMAGE", "pi_oj-judge:latest")
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
GEMINI_API_URL = os.getenv("GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


def normalize_output(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def normalize_output_without_blank_lines(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n") if line.strip())


def truncate_context(value: str, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...（內容過長，已截斷）"


def extract_openai_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    chunks = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def extract_gemini_text(data: dict) -> str:
    chunks = []
    for candidate in data.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            if isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def download_uva_pdf_text(problem_number: int) -> tuple[str, str]:
    from pypdf import PdfReader

    pdf_url = f"https://onlinejudge.org/external/{problem_number // 100}/{problem_number}.pdf"
    try:
        response = requests.get(
            pdf_url,
            headers={"User-Agent": "Pi-OJ/1.0 (+https://onlinejudge.org/)"},
            timeout=(5, 60),
        )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise RuntimeError("UVA 官方 PDF 下載逾時（60 秒），請稍後再試") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"UVA 官方 PDF 下載失敗：{exc}") from exc

    if not response.content.startswith(b"%PDF"):
        raise RuntimeError("UVA 回應不是有效的 PDF，可能是題號不存在或網站暫時阻擋")

    reader = PdfReader(BytesIO(response.content))

    def extract_pdf_text(mode: str) -> str:
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text(extraction_mode=mode) or "")
            except TypeError:
                chunks.append(page.extract_text() or "")
        return "\n\f\n".join(chunks).replace("\r\n", "\n").replace("\r", "\n")

    return extract_pdf_text("layout"), extract_pdf_text("plain")


def parse_codeforces_problem_id(value: str) -> tuple[str, str]:
    text = (value or "").strip().upper().replace("-", "/")
    match = re.fullmatch(r"(\d{1,6})\s*/?\s*([A-Z][0-9A-Z]?)", text)
    if not match:
        raise ValueError("Codeforces problem id must look like 1843A or 1843/A")
    return match.group(1), match.group(2)


def codeforces_problem_url(problem_id: str) -> str:
    contest_id, index = parse_codeforces_problem_id(problem_id)
    return f"https://codeforces.com/problemset/problem/{contest_id}/{index}"


def codeforces_pre_text(pre) -> str:
    html = str(pre).replace("<br/>", "\n").replace("<br>", "\n").replace("<br />", "\n")
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def fetch_codeforces_problem_page(problem_id: str) -> BeautifulSoup:
    url = codeforces_problem_url(problem_id)
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Pi-OJ/1.0)"},
        timeout=(5, 30),
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def extract_codeforces_problem_statement(problem_id: str) -> str:
    soup = fetch_codeforces_problem_page(problem_id)
    statement = soup.select_one(".problem-statement")
    if statement is None:
        raise RuntimeError("Cannot find Codeforces problem statement")
    for block in statement.select(".sample-tests, script, style"):
        block.decompose()
    title = statement.select_one(".title")
    header = title.get_text(" ", strip=True) if title else f"Codeforces {problem_id}"
    text = statement.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return f"{header}\n\n{text}"[:80_000]


def render_codeforces_problem_statement_html(problem_id: str) -> str:
    soup = fetch_codeforces_problem_page(problem_id)
    statement = soup.select_one(".problem-statement")
    if statement is None:
        raise RuntimeError("Cannot find Codeforces problem statement")
    for block in statement.select("script, style"):
        block.decompose()
    title_node = statement.select_one(".title")
    title = title_node.get_text(" ", strip=True) if title_node else f"Codeforces {problem_id}"
    html = str(statement)
    html = re.sub(r"\$\$\$(.*?)\$\$\$", r"<code>\1</code>", html, flags=re.S)
    html = re.sub(r"\$\$(.*?)\$\$", r"<code>\1</code>", html, flags=re.S)
    html = html.replace('\\leq', '&le;').replace('\\le', '&le;').replace('\\geq', '&ge;').replace('\\ge', '&ge;')
    html = html.replace('\\dots', '&hellip;').replace('\\cdot', '&middot;')
    html = re.sub(r"\\color\{[^}]+\}\{([^{}]*)\}", r"\1", html)
    html = re.sub(r"\\(?:max|min)\b", lambda m: m.group(0)[1:], html)
    return f'''<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; padding: 24px 30px 36px; font: 15px/1.72 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #18231f; background: #fff; }}
    .problem-statement {{ max-width: 900px; margin: 0 auto; }}
    .header {{ margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid #dfe6e1; text-align: center; }}
    .title {{ font-size: 22px; font-weight: 750; margin-bottom: 10px; }}
    .time-limit, .memory-limit, .input-file, .output-file {{ color: #52615a; font-size: 13px; line-height: 1.5; }}
    p {{ margin: 11px 0; }}
    .section-title {{ margin: 24px 0 8px; font-weight: 750; font-size: 18px; }}
    code {{ padding: 1px 4px; border-radius: 4px; background: #f3f6f4; font-family: "Cascadia Code", Consolas, monospace; font-size: 0.94em; }}
    pre {{ margin: 8px 0 14px; padding: 12px 14px; border: 1px solid #dce5df; border-radius: 7px; background: #f8faf8; white-space: pre-wrap; word-break: break-word; font-family: "Cascadia Code", Consolas, monospace; font-size: 13px; }}
    .sample-tests .input, .sample-tests .output {{ margin: 12px 0; }}
    .sample-tests .title {{ margin: 0 0 6px; text-align: left; font-size: 14px; font-weight: 700; }}
  </style>
</head>
<body>
{html}
</body>
</html>'''


def fetch_codeforces_sample(problem_id: str) -> dict:
    soup = fetch_codeforces_problem_page(problem_id)
    sample = soup.select_one(".sample-tests")
    if sample is None:
        raise RuntimeError("Codeforces page has no sample tests")
    inputs = [codeforces_pre_text(pre) for pre in sample.select(".input pre")]
    outputs = [codeforces_pre_text(pre) for pre in sample.select(".output pre")]
    if not inputs or not outputs:
        raise RuntimeError("Cannot parse Codeforces sample input/output")
    return {
        "status": "FETCHED",
        "source": "sample",
        "judge": "codeforces",
        "problem_id": problem_id,
        "input_data": "\n".join(inputs)[:1_000_000],
        "expected_output": "\n".join(outputs)[:1_000_000],
        "message": "Loaded sample tests from Codeforces problem page",
    }


def html_escape(value: str) -> str:
    import html
    return html.escape(value, quote=False)


def fetch_problem_statement_html(judge: str, problem_id: str) -> str:
    normalized_judge = (judge or "uva").lower()
    if normalized_judge == "codeforces":
        return render_codeforces_problem_statement_html(problem_id)
    else:
        statement = extract_uva_problem_statement(validate_problem_number(problem_id))
        title = f"UVA {problem_id}"
    body = html_escape(statement)
    return f"""<!doctype html>
<html lang=\"zh-Hant\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html_escape(title)}</title>
  <style>
    body {{ margin: 0; padding: 22px 26px; font: 14px/1.65 system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #1c2521; background: #fff; }}
    h1 {{ margin: 0 0 16px; font-size: 20px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; font-family: 'Cascadia Code', Consolas, monospace; }}
  </style>
</head>
<body>
  <h1>{html_escape(title)}</h1>
  <pre>{body}</pre>
</body>
</html>"""


def is_pdf_noise_line(line: str) -> bool:
    compact = re.sub(r"\s+", " ", line).strip()
    if not compact:
        return False
    return bool(
        re.search(r"\bOJ\s*:\s*\d+\b.*\d+\s*/\s*\d+\s*$", compact, re.I)
        or re.search(r"Universidad\s+de\s+V\s*alladolid\s+OJ", compact, re.I)
        or re.search(r"UVa\s+Online\s+Judge.*\d+\s*/\s*\d+\s*$", compact, re.I)
        or re.search(r"^\d+\s*/\s*\d+\s*$", compact)
        or re.search(r"^\d+\s*[-–]\s+.+\s+\d+\s*/\s*\d+\s*$", compact)
        or re.fullmatch(r"\d+\s*/\s*\d+", compact)
    )


def clean_problem_statement_text(value: str) -> str:
    lines = []
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").split("\n"):
        line = raw_line.strip()
        if not line or is_pdf_noise_line(line):
            continue
        lines.append(re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(lines).strip()


def extract_uva_problem_statement(problem_number: int) -> str:
    layout_text, plain_text = download_uva_pdf_text(problem_number)
    text = layout_text if re.search(r"(?im)^\s*Sample\s+Input\s*$", layout_text) else plain_text
    sample_heading = re.search(r"(?im)^\s*Sample\s+Input\s*$", text)
    if sample_heading:
        text = text[:sample_heading.start()]
    statement = clean_problem_statement_text(text)
    if not statement:
        raise RuntimeError("無法從 UVA PDF 抽出題目敘述")
    return statement[:80_000]


def build_ai_prompt(
    mode: str,
    judge: str,
    problem_id: str,
    language: str,
    code: str,
    input_data: str,
    expected_output: str,
    stdout: str,
    error_log: str,
    verdict: str,
    question: str,
    problem_statement: str = "",
) -> str:
    judge_name = "Codeforces" if (judge or "uva").lower() == "codeforces" else "UVA"
    common_context = f"""Online judge: {judge_name}
Problem: {problem_id or "not provided"}
Language: {language}
Verdict: {verdict or "not run yet"}

User question:
```text
{truncate_context(question, 4_000) or "(none)"}
```

Current code:
```{language}
{truncate_context(code)}
```

stdin:
```text
{truncate_context(input_data)}
```

expected stdout:
```text
{truncate_context(expected_output)}
```

actual stdout:
```text
{truncate_context(stdout)}
```

error log:
```text
{truncate_context(error_log, 6_000)}
```
"""

    if mode == "translate":
        return f"""You are translating an online judge problem statement for a Traditional Chinese reader.

Task:
- Translate the problem statement into clear Traditional Chinese.
- Preserve section names, mathematical notation, variable names, and limits accurately.
- Do not solve the problem and do not provide code.
- If the statement contains input/output format details, make them easy to understand.
- If the user wrote an extra question, answer only if it is about understanding the statement.

Online judge: {judge_name}
Problem: {problem_id}
User question:
```text
{truncate_context(question, 4_000) or "(none)"}
```

Problem statement extracted from the judge page:
```text
{truncate_context(problem_statement, 70_000)}
```
"""

    if mode == "expected":
        return f"""You are an Online Judge output oracle. Use the online judge problem statement to compute the exact expected stdout for the user's provided stdin.

Task:
- Answer in Traditional Chinese.
- Do not create new input. Use the provided stdin exactly as-is.
- Compute the exact expected stdout for that stdin according to the online judge problem statement.
- Do not provide a full accepted solution or code.
- If the stdin is invalid for the problem, explain the issue instead of inventing output.
- If you cannot confidently compute the output, say so clearly instead of guessing.

Required output format. Put this fenced block first:
expected stdout:
```text
...
```

Online judge: {judge_name}
Problem: {problem_id}
User-provided stdin:
```text
{truncate_context(input_data, 20_000) or "(empty)"}
```

User request:
```text
{truncate_context(question, 4_000) or "Please compute the expected stdout for this stdin."}
```

Problem statement extracted from the judge page:
```text
{truncate_context(problem_statement, 70_000)}
```
"""

    if mode == "tests":
        return f"""You are an Online Judge test-data assistant. Use the online judge problem statement to create local test cases.

Task:
- Answer in Traditional Chinese.
- Create one combined local test input and its exact expected stdout.
- Prefer edge cases and bug-hunting cases.
- Keep the generated test small enough to inspect by hand.
- Do not provide a full accepted solution or code.
- If you cannot confidently compute expected output, say so instead of inventing one.

Required output format. Put these two fenced blocks first:
stdin:
```text
...
```
expected stdout:
```text
...
```

Online judge: {judge_name}
Problem: {problem_id}
User request:
```text
{truncate_context(question, 4_000) or "Please generate one bug-catching test case."}
```

Problem statement extracted from the judge page:
```text
{truncate_context(problem_statement, 70_000)}
```

Current code, for bug-focused tests only:
```{language}
{truncate_context(code, 20_000)}
```
"""

    if mode == "ask":
        return f"""You are an Online Judge programming tutor. Answer the user's custom prompt in Traditional Chinese.

Rules:
- Answer the user's custom prompt directly; do not apply the hint/debug/translation templates.
- The user's question has priority over the current problem context.
- If the user asks a general programming or algorithm question, answer only that question.
- Do not mention, compare with, or analyze the current online judge problem unless the user explicitly asks about the current problem, their code, a verdict, stdin/stdout, sample tests, or debugging.
- Use the current problem, code, input, output, and error log only when the user's words clearly need that context.
- If the user asks for a template, provide a compact reusable code template and a short explanation.
- Do not start with greetings or self-introduction.
- Prefer this structure when suitable: short definition, when to use it, template, common pitfalls.
- Put code templates in fenced code blocks.
- Do not reveal a full accepted solution unless the user explicitly asks for it; prefer explanation and next steps.
- Format the answer with short sections or bullets for readability.

Optional context. Ignore this section unless it is clearly relevant:
{common_context}
"""

    if mode == "hint":
        instruction = """Help the user understand the problem and find a solution direction.
- Mention the likely core observation or algorithm.
- Include useful complexity or data structure hints.
- Do not provide a full accepted solution.
- Suggested structure: statement summary, idea, traps, next step."""
    else:
        instruction = """Help the user debug the current program.
- Prioritize verdict, stdout, expected stdout, error log, and code.
- Point out the most likely 1 to 3 causes.
- Suggest targeted tests or variables to inspect.
- Do not rewrite the whole accepted solution."""

    return f"""You are an Online Judge programming tutor. Answer in Traditional Chinese, clearly and concisely.

{instruction}

{common_context}
"""


def run_container(host_dir: Path, command: list[str], stdin_data: str = "", timeout: int = 5) -> tuple[int, str, str]:
    client = docker.from_env()
    container = None
    try:
        container = client.containers.create(
            JUDGE_IMAGE,
            command=command,
            working_dir="/sandbox",
            user="1001:1001",
            network_disabled=True,
            mem_limit="128m",
            memswap_limit="128m",
            nano_cpus=500_000_000,
            pids_limit=64,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            volumes={str(host_dir): {"bind": "/sandbox", "mode": "rw"}},
            stdin_open=True,
        )
        sock = container.attach_socket(params={"stdin": 1, "stream": 1})
        container.start()
        if stdin_data:
            sock._sock.sendall(stdin_data.encode("utf-8"))
        # 即使沒有測資也要送出 EOF，否則讀到 EOF 的程式會卡到 TLE。
        sock._sock.shutdown(1)
        result = container.wait(timeout=timeout)
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        return int(result["StatusCode"]), stdout, stderr
    except requests.exceptions.ReadTimeout:
        if container:
            container.kill()
        return 124, "", "Time limit exceeded"
    except requests.exceptions.ConnectionError as exc:
        if "read timed out" not in str(exc).lower():
            raise
        if container:
            container.kill()
        return 124, "", "Time limit exceeded"
    finally:
        if container:
            container.remove(force=True)
        client.close()


@worker.task
def judge_submission_task(submission_id: str, code: str, language: str, input_data: str, expected_output: str) -> dict:
    language_aliases = {
        "cpp": "cf_cpp17", "cpp17": "cf_cpp17", "cpp20": "cf_cpp20", "c": "cf_c11", "py": "cf_py313", "java": "cf_java21", "js": "cf_node",
        "uva_c": "c", "uva_java": "java", "uva_pascal": "pascal", "uva_cpp11": "cpp11", "uva_py3": "py",
        "cf_c11": "c", "cf_cpp17": "cpp17", "cf_cpp20": "cpp20", "cf_cpp23": "cpp23", "cf_java21": "java", "cf_py313": "py", "cf_pypy310": "pypy", "cf_js_v8": "js_v8", "cf_node": "js",
        "cf_rust2021": "rust2021", "cf_rust2024": "rust2024", "cf_go": "go", "cf_perl": "perl", "cf_php": "php", "cf_ruby": "ruby", "cf_ocaml": "ocaml", "cf_d": "d",
    }
    runner_language = language_aliases.get(language)
    unsupported_local = {}
    if not runner_language:
        label = unsupported_local.get(language, language)
        return {"submission_id": submission_id, "status": "SE", "error_log": f"Local runner for {label} is not installed yet"}
    language_specs = {
        "c": {"filename": "solution.c", "compile": ["gcc", "-std=c11", "-O2", "-pipe", "solution.c", "-lm", "-o", "solution"], "run": ["./solution"]},
        "pascal": {"filename": "solution.pas", "compile": ["fpc", "-O2", "-osolution", "solution.pas"], "run": ["./solution"]},
        "cpp11": {"filename": "solution.cpp", "compile": ["g++", "-std=c++11", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "cpp17": {"filename": "solution.cpp", "compile": ["g++", "-std=c++17", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "cpp20": {"filename": "solution.cpp", "compile": ["g++", "-std=c++20", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "cpp23": {"filename": "solution.cpp", "compile": ["g++", "-std=c++23", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "py": {"filename": "solution.py", "run": ["python3", "-I", "-B", "solution.py"]},
        "pypy": {"filename": "solution.py", "run": ["pypy3", "solution.py"]},
        "java": {"filename": "Main.java", "compile": ["javac", "Main.java"], "run": ["java", "Main"]},
        "js": {"filename": "solution.js", "run": ["node", "solution.js"]},
        "js_v8": {"filename": "solution.js", "extra_files": {"cf_v8_runner.js": "const fs = require('fs');\nconst lines = fs.readFileSync(0, 'utf8').replace(/\\r\\n/g, '\\n').split('\\n');\nlet index = 0;\nglobal.readline = () => { if (index >= lines.length) throw new Error('EOF'); return lines[index++]; };\nrequire('./solution.js');\n"}, "run": ["node", "cf_v8_runner.js"]},
        "perl": {"filename": "solution.pl", "run": ["perl", "solution.pl"]},
        "php": {"filename": "solution.php", "run": ["php", "solution.php"]},
        "ruby": {"filename": "solution.rb", "run": ["ruby", "solution.rb"]},
        "ocaml": {"filename": "solution.ml", "compile": ["ocamlopt", "-O3", "solution.ml", "-o", "solution"], "run": ["./solution"]},
        "d": {"filename": "solution.d", "compile": ["ldc2", "-O2", "solution.d", "-of=solution"], "run": ["./solution"]},
        "rust2021": {"filename": "solution.rs", "compile": ["rustc", "--edition=2021", "-O", "solution.rs", "-o", "solution"], "run": ["./solution"]},
        "rust2024": {"filename": "solution.rs", "compile": ["rustc", "--edition=2024", "-O", "solution.rs", "-o", "solution"], "run": ["./solution"]},
        "go": {"filename": "solution.go", "compile": ["go", "build", "-o", "solution", "solution.go"], "run": ["./solution"]},
    }
    spec = language_specs.get(runner_language)
    if not spec:
        return {"submission_id": submission_id, "status": "SE", "error_log": "Unsupported language"}

    container_dir = SANDBOX_ROOT / submission_id
    host_dir = HOST_SANDBOX_ROOT / submission_id
    container_dir.mkdir(mode=0o777, parents=True, exist_ok=False)
    os.chmod(container_dir, 0o777)
    started = time.monotonic()

    try:
        filename = spec["filename"]
        (container_dir / filename).write_text(code, encoding="utf-8")
        os.chmod(container_dir / filename, 0o644)
        for extra_name, extra_content in spec.get("extra_files", {}).items():
            (container_dir / extra_name).write_text(extra_content, encoding="utf-8")
            os.chmod(container_dir / extra_name, 0o644)

        if spec.get("compile"):
            exit_code, _, compile_error = run_container(host_dir, spec["compile"], timeout=30)
            if exit_code != 0:
                return {"submission_id": submission_id, "status": "CE", "stdout": "", "error_log": compile_error[:20_000]}
        command = spec["run"]

        exit_code, stdout, stderr = run_container(host_dir, command, input_data, timeout=5)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = ""
        if exit_code == 124:
            status = "TLE"
        elif exit_code != 0:
            status = "RE"
        elif expected_output:
            stdout_normalized = normalize_output(stdout)
            expected_normalized = normalize_output(expected_output)
            if stdout_normalized == expected_normalized:
                status = "AC"
            elif normalize_output_without_blank_lines(stdout) == normalize_output_without_blank_lines(expected_output):
                status = "AC"
                message = "Only blank lines differ; accepted as AC."
            else:
                status = "WA"
        elif not expected_output:
            status = "OK"
        else:
            status = "AC"

        payload = {"submission_id": submission_id, "status": status, "cpu_time": f"{elapsed_ms}ms", "stdout": stdout[:100_000], "error_log": stderr[:20_000]}
        if message:
            payload["message"] = message
        return payload
    except ImageNotFound:
        return {"submission_id": submission_id, "status": "SE", "error_log": f"Judge image {JUDGE_IMAGE} was not found; build the judge image first"}
    except ContainerError as exc:
        return {"submission_id": submission_id, "status": "SE", "error_log": str(exc)}
    except Exception as exc:
        return {"submission_id": submission_id, "status": "SE", "error_log": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(container_dir, ignore_errors=True)


class UvaClient:
    BASE_URL = "https://onlinejudge.org"

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        # UVA 使用較舊的 Joomla/Community Builder 登入模組；使用一般瀏覽器標頭
        # 可避免部分站台層或模組層把非瀏覽器 client 當成無效登入。
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    @staticmethod
    def hidden_fields(container) -> dict[str, str]:
        return {
            element.get("name"): element.get("value", "")
            for element in container.select('input[type="hidden"][name]')
        }

    @staticmethod
    def login_form(soup: BeautifulSoup):
        forms = soup.select('form[action*="com_comprofiler"][action*="task=login"]')
        for form in forms:
            if form.select_one('input[name="username"]') and form.select_one('input[name="passwd"]'):
                return form
        return None

    @staticmethod
    def page_error(soup: BeautifulSoup) -> str:
        candidates = soup.select("#system-message, .error, .message, .cbErrorMsg")
        messages = [element.get_text(" ", strip=True) for element in candidates]
        return " | ".join(message for message in messages if message)[:500]

    def login(self) -> None:
        url = f"{self.BASE_URL}/index.php?option=com_comprofiler&task=login"
        if os.getenv("UVA_LOGIN_MODE", "browser").lower() == "browser":
            self.login_with_browser(url)
            return

        page = self.session.get(url, timeout=20)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        form = self.login_form(soup)
        if form is None:
            raise RuntimeError("找不到 UVA 登入表單；網站流程可能已改變")

        payload = self.hidden_fields(form)
        payload.update({
            "username": self.username,
            "passwd": self.password,
            "op2": "login",
            "lang": "english",
            "force_session": "1",
            "remember": "yes",
            "Submit": "Login",
        })
        action = urljoin(page.url, form.get("action") or url)
        response = self.session.post(
            action,
            data=payload,
            headers={"Referer": page.url, "Origin": self.BASE_URL},
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()

        # POST 回應不一定包含 Logout，因此再讀一次首頁驗證 session。
        verify = self.session.get(f"{self.BASE_URL}/", timeout=20)
        verify.raise_for_status()
        verify_soup = BeautifulSoup(verify.text, "html.parser")
        if self.login_form(verify_soup) is not None:
            # UVA 對部分非瀏覽器登入回傳通用帳密錯誤。使用真正的 Chromium
            # 完成登入，再把 cookie 複製回 requests session 供提交流程使用。
            self.login_with_browser(url)

    def login_with_browser(self, url: str) -> None:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import StaleElementReferenceException, UnexpectedAlertPresentException
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise RuntimeError("HTTP 登入失敗，且 worker 尚未安裝 Selenium") from exc

        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        driver_path = shutil.which("chromedriver")
        if not chromium_path:
            raise RuntimeError("worker 內找不到 Chromium；請確認 Dockerfile 已安裝 chromium")
        if not driver_path:
            raise RuntimeError("worker 內找不到 chromedriver；請確認 Dockerfile 已安裝 chromium-driver")

        options = webdriver.ChromeOptions()
        options.binary_location = chromium_path
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,900")
        options.add_argument(f"--user-agent={self.session.headers['User-Agent']}")

        driver = None
        try:
            driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
            driver.set_page_load_timeout(30)
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd(
                "Network.setBlockedURLs",
                {
                    "urls": [
                        "*://*.doubleclick.net/*",
                        "*://*.googlesyndication.com/*",
                        "*://googleads.g.doubleclick.net/*",
                    ]
                },
            )
            driver.get(url)

            try:
                login_result = driver.execute_script(
                    """
                    const username = arguments[0];
                    const password = arguments[1];
                    const form = Array.from(document.forms).find((item) =>
                        item.querySelector('[name="username"]') &&
                        item.querySelector('[name="passwd"]') &&
                        item.getClientRects().length > 0
                    );
                    if (!form) return {ok: false, error: '找不到可見的 UVA 登入表單'};
                    const usernameInput = form.querySelector('[name="username"]');
                    const passwordInput = form.querySelector('[name="passwd"]');
                    const submitButton = form.querySelector('[name="Submit"], input[type="submit"], button[type="submit"]');
                    if (!submitButton) return {ok: false, error: '找不到 UVA 登入按鈕'};
                    usernameInput.value = username;
                    passwordInput.value = password;
                    usernameInput.dispatchEvent(new Event('input', {bubbles: true}));
                    passwordInput.dispatchEvent(new Event('input', {bubbles: true}));
                    setTimeout(() => {
                        if (typeof form.requestSubmit === 'function') form.requestSubmit(submitButton);
                        else form.submit();
                    }, 0);
                    return {ok: true};
                    """,
                    self.username,
                    self.password,
                )
            except StaleElementReferenceException:
                # 已觸發登入導頁時，舊文件失效是預期結果。
                login_result = {"ok": True}
            if login_result and not login_result.get("ok"):
                raise RuntimeError(login_result.get("error", "UVA 登入表單設定失敗"))

            def login_finished(browser) -> bool:
                try:
                    has_visible_form = browser.execute_script(
                        """
                        return Array.from(document.forms).some((item) =>
                            item.querySelector('[name="username"]') &&
                            item.querySelector('[name="passwd"]') &&
                            item.getClientRects().length > 0
                        );
                        """
                    )
                    source = browser.page_source.lower()
                    return not has_visible_form or "incorrect username or password" in source
                except StaleElementReferenceException:
                    return False

            try:
                WebDriverWait(driver, 20).until(login_finished)
            except UnexpectedAlertPresentException:
                alert = driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                raise RuntimeError(f"UVA 瀏覽器拒絕登入：{alert_text}")
            page_soup = BeautifulSoup(driver.page_source, "html.parser")
            if self.login_form(page_soup) is not None:
                detail = self.page_error(page_soup)
                suffix = f"；UVA 訊息：{detail}" if detail else ""
                raise RuntimeError(f"UVA 瀏覽器登入仍失敗{suffix}")

            self.session.cookies.clear()
            for cookie in driver.get_cookies():
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain") or "onlinejudge.org",
                    path=cookie.get("path", "/"),
                )

            verify = self.session.get(f"{self.BASE_URL}/", timeout=20)
            verify.raise_for_status()
            if self.login_form(BeautifulSoup(verify.text, "html.parser")) is not None:
                raise RuntimeError("Chromium 已登入，但 session cookie 無法交給提交程序")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"UVA Chromium 登入錯誤：{type(exc).__name__}: {exc}") from exc
        finally:
            if driver is not None:
                driver.quit()

    def submit(self, problem_id: str, language: str, code: str) -> dict:
        self.login()
        if os.getenv("UVA_LOGIN_MODE", "browser").lower() == "browser":
            return self.submit_with_browser(problem_id, language, code)

        page_url = f"{self.BASE_URL}/index.php?option=com_onlinejudge&Itemid=25&page=submit_problem"
        page = self.session.get(page_url, timeout=20)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        submit_form = next(
            (form for form in soup.select("form") if form.select_one('[name="code"]') or form.select_one('[name="localid"]')),
            None,
        )
        if submit_form is None:
            raise RuntimeError("已登入，但找不到 UVA 提交表單；提交頁面可能已改版")
        payload = self.hidden_fields(submit_form)
        language = {
            "cpp": "uva_cpp11", "cpp17": "uva_cpp11", "cpp20": "uva_cpp11", "c": "uva_c", "py": "uva_py3", "java": "uva_java",
        }.get(language, language)
        uva_language_ids = {
            "uva_c": os.getenv("UVA_C_LANGUAGE_ID", "1"),
            "uva_java": os.getenv("UVA_JAVA_LANGUAGE_ID", "2"),
            "uva_pascal": os.getenv("UVA_PASCAL_LANGUAGE_ID", "3"),
            "uva_cpp11": os.getenv("UVA_CPP_LANGUAGE_ID", "5"),
            "uva_py3": os.getenv("UVA_PY_LANGUAGE_ID", "6"),
        }
        language_id = uva_language_ids.get(language, "")
        if not language_id:
            raise RuntimeError(f"UVA submit language id is not configured for {language}")
        payload.update({
            "localid": problem_id,
            "language": language_id,
            "code": code,
            "task": "save_submission",
        })
        response = self.session.post(
            f"{self.BASE_URL}/index.php?option=com_onlinejudge&Itemid=25&task=save_submission",
            data=payload,
            timeout=30,
        )
        response.raise_for_status()
        body = response.text.lower()
        failure_words = ("invalid problem", "not authorized", "please login", "submission error")
        if any(word in body for word in failure_words):
            raise RuntimeError("UVA 未確認提交成功，請查看 worker log；網站表單可能已改版")
        # HTTP 模式無法可靠確認 UVA 是否真的建立 submission，不回報假成功。
        return {
            "status": "UNKNOWN",
            "problem_id": problem_id,
            "message": "UVA 已接收 HTTP 請求，但無法確認是否建立 submission；請使用 browser 模式",
        }

    def submit_with_browser(self, problem_id: str, language: str, code: str) -> dict:
        from selenium import webdriver
        from selenium.common.exceptions import (
            NoAlertPresentException,
            StaleElementReferenceException,
            TimeoutException,
        )
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait

        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        driver_path = shutil.which("chromedriver")
        if not chromium_path or not driver_path:
            raise RuntimeError("worker 缺少 Chromium 或 chromedriver")

        options = webdriver.ChromeOptions()
        options.binary_location = chromium_path
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,900")
        options.add_argument(f"--user-agent={self.session.headers['User-Agent']}")

        driver = None

        def parse_submission_row(row) -> dict | None:
            cells = row.find_elements(By.CSS_SELECTOR, "td")
            if len(cells) < 6 or not cells[0].text.strip().isdigit():
                return None

            # UVA 的 Problem 表頭跨兩欄（題號＋標題），因此從尾端定位較可靠：
            # [...problem cells, verdict, language, run time, submission date]
            verdict_index = len(cells) - 4
            return {
                "id": cells[0].text.strip(),
                "problem": " ".join(cell.text.strip() for cell in cells[1:verdict_index]),
                "verdict": cells[-4].text.strip(),
                "language": cells[-3].text.strip(),
                "run_time": cells[-2].text.strip(),
            }

        def newest_submission() -> dict | None:
            for _ in range(2):
                try:
                    for row in driver.find_elements(By.CSS_SELECTOR, "table tr"):
                        submission = parse_submission_row(row)
                        if submission:
                            return submission
                except StaleElementReferenceException:
                    continue
            return None

        def submission_by_id(submission_id: str) -> dict | None:
            for _ in range(2):
                try:
                    for row in driver.find_elements(By.CSS_SELECTOR, "table tr"):
                        submission = parse_submission_row(row)
                        if submission and submission["id"] == submission_id:
                            return submission
                except StaleElementReferenceException:
                    continue
            return None

        def verdict_status(verdict: str) -> str | None:
            normalized = " ".join(verdict.lower().split())
            if not normalized or any(
                marker in normalized
                for marker in ("queue", "pending", "running", "judging", "received", "compiling", "executing")
            ):
                return None

            mappings = (
                ("accepted", "AC"),
                ("wrong answer", "WA"),
                ("time limit", "TLE"),
                ("runtime error", "RE"),
                ("compilation error", "CE"),
                ("presentation error", "PE"),
                ("memory limit", "MLE"),
                ("output limit", "OLE"),
                ("restricted function", "RF"),
                ("can't be judged", "CJ"),
                ("cannot be judged", "CJ"),
            )
            return next((status for marker, status in mappings if marker in normalized), "JUDGED")

        stage = "啟動 Chromium"
        try:
            driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
            driver.set_page_load_timeout(30)
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd(
                "Network.setBlockedURLs",
                {
                    "urls": [
                        "*://*.doubleclick.net/*",
                        "*://*.googlesyndication.com/*",
                        "*://googleads.g.doubleclick.net/*",
                    ]
                },
            )

            stage = "載入登入 cookie"
            # 先進入同網域，再加入登入階段取得的 cookies。
            driver.get(f"{self.BASE_URL}/")
            for cookie in self.session.cookies:
                item = {
                    "name": cookie.name,
                    "value": cookie.value,
                    "path": cookie.path or "/",
                }
                if cookie.domain:
                    item["domain"] = cookie.domain
                driver.add_cookie(item)
            driver.refresh()

            submission_links = driver.find_elements(By.PARTIAL_LINK_TEXT, "My Submissions")
            my_submissions_url = next(
                (element.get_attribute("href") for element in submission_links if element.get_attribute("href")),
                None,
            )
            if not my_submissions_url:
                raise RuntimeError("登入 cookie 已失效，找不到 My Submissions")

            stage = "讀取提交前紀錄"
            driver.get(my_submissions_url)
            previous = newest_submission()
            previous_id = previous["id"] if previous else None

            submit_url = f"{self.BASE_URL}/index.php?option=com_onlinejudge&Itemid=25&page=submit_problem"
            stage = "載入提交表單"
            driver.get(submit_url)
            language = {
                "cpp": "uva_cpp11", "cpp17": "uva_cpp11", "cpp20": "uva_cpp11", "c": "uva_c", "py": "uva_py3", "java": "uva_java",
            }.get(language, language)
            uva_language_ids = {
                "uva_c": os.getenv("UVA_C_LANGUAGE_ID", "1"),
                "uva_java": os.getenv("UVA_JAVA_LANGUAGE_ID", "2"),
                "uva_pascal": os.getenv("UVA_PASCAL_LANGUAGE_ID", "3"),
                "uva_cpp11": os.getenv("UVA_CPP_LANGUAGE_ID", "5"),
                "uva_py3": os.getenv("UVA_PY_LANGUAGE_ID", "6"),
            }
            language_id = uva_language_ids.get(language, "")
            if not language_id:
                raise RuntimeError(f"UVA submit language id is not configured for {language}")
            submit_page_url = driver.current_url
            stage = "送出 UVA 表單"
            try:
                form_result = driver.execute_script(
                    """
                    const problemId = arguments[0];
                    const languageId = arguments[1];
                    const sourceCode = arguments[2];
                    const form = Array.from(document.forms).find((item) =>
                        item.elements.namedItem('localid') &&
                        item.elements.namedItem('language') &&
                        item.elements.namedItem('code')
                    );
                    if (!form) return {ok: false, error: '找不到 UVA 提交表單'};

                    const problem = form.elements.namedItem('localid');
                    const code = form.elements.namedItem('code');
                    problem.value = problemId;
                    code.value = sourceCode;

                    const languageFields = Array.from(form.querySelectorAll('[name="language"]'));
                    if (!languageFields.length) return {ok: false, error: '缺少 language 欄位'};
                    if (languageFields[0].tagName.toLowerCase() === 'select') {
                        languageFields[0].value = languageId;
                        if (languageFields[0].value !== languageId) {
                            return {ok: false, error: `找不到語言代碼 ${languageId}`};
                        }
                        languageFields[0].dispatchEvent(new Event('change', {bubbles: true}));
                    } else {
                        const target = languageFields.find((field) => field.value === languageId) ||
                            (languageFields.length === 1 ? languageFields[0] : null);
                        if (!target) {
                            return {ok: false, error: `找不到語言代碼 ${languageId}；可用值：${languageFields.map((f) => f.value).join(',')}`};
                        }
                        if (target.type === 'radio' || target.type === 'checkbox') target.checked = true;
                        else target.value = languageId;
                        target.dispatchEvent(new Event('change', {bubbles: true}));
                    }

                    problem.dispatchEvent(new Event('input', {bubbles: true}));
                    code.dispatchEvent(new Event('input', {bubbles: true}));
                    const button = form.querySelector('input[type="submit"], button[type="submit"]');
                    if (!button) return {ok: false, error: '找不到 UVA 提交按鈕'};

                    setTimeout(() => {
                        if (typeof form.requestSubmit === 'function') form.requestSubmit(button);
                        else form.submit();
                    }, 0);
                    return {ok: true};
                    """,
                    problem_id,
                    language_id,
                    code,
                )
            except StaleElementReferenceException:
                # 表單已送出並導頁時，舊頁面失效是預期結果。
                pass
            else:
                if form_result and not form_result.get("ok"):
                    raise RuntimeError(form_result.get("error", "UVA 提交表單設定失敗"))
            try:
                WebDriverWait(driver, 15).until(lambda browser: browser.current_url != submit_page_url)
            except TimeoutException:
                # 某些 UVA 回應會回到相同 URL；後續仍以 My Submissions 驗證。
                pass

            try:
                alert = driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                raise RuntimeError(f"UVA 拒絕提交：{alert_text}")
            except NoAlertPresentException:
                pass

            # 第一階段：只有 My Submissions 出現新 ID 才算真正送出。
            stage = "確認新 submission ID"
            created = None
            for _ in range(15):
                driver.get(my_submissions_url)
                current = newest_submission()
                if current and current["id"] != previous_id:
                    created = current
                    break
                time.sleep(2)

            if created is None:
                raise RuntimeError("提交表單已送出，但 My Submissions 在 30 秒內沒有出現新紀錄")

            # 第二階段：鎖定同一個 ID 等待最終 verdict，避免抓到其他提交。
            stage = "等待 UVA 最終判題"
            submission_id = created["id"]
            for _ in range(30):
                driver.get(my_submissions_url)
                current = submission_by_id(submission_id)
                if current:
                    status = verdict_status(current["verdict"])
                    if status is not None:
                        return {
                            "status": status,
                            "problem_id": problem_id,
                            "uva_submission_id": submission_id,
                            "verdict": current["verdict"],
                            "cpu_time": current["run_time"],
                            "message": f"UVA #{submission_id}：{current['verdict']}",
                        }
                time.sleep(2)

            return {
                "status": "SUBMITTED",
                "problem_id": problem_id,
                "uva_submission_id": submission_id,
                "verdict": created["verdict"],
                "message": f"UVA 已建立 submission #{submission_id}，但 60 秒內尚未完成判題",
            }
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"UVA 瀏覽器提交錯誤（階段：{stage}）：{type(exc).__name__}: {exc}") from exc
        finally:
            if driver is not None:
                driver.quit()


def validate_problem_number(problem_id: str) -> int:
    try:
        problem_number = int(problem_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("UVA 題號格式錯誤") from exc
    if not (100 <= problem_number <= 1800 or 10_000 <= problem_number <= 13_400):
        raise ValueError("UVA 題號必須介於 100–1800 或 10000–13400")
    return problem_number


def fetch_uva_sample(problem_number: int) -> dict:
    layout_text, plain_text = download_uva_pdf_text(problem_number)

    def has_sample_headings(value: str) -> bool:
        return bool(
            re.search(r"(?im)^\s*Sample\s+Input\s*$", value)
            and re.search(r"(?im)^\s*Sample\s+Output\s*$", value)
        )

    # layout 模式比較會保留 PDF 裡的垂直間距；plain 當備援，避免少數舊 PDF 抽不到標題。
    text = layout_text if has_sample_headings(layout_text) else plain_text
    input_heading = re.search(r"(?im)^\s*Sample\s+Input\s*$", text)
    output_heading = re.search(r"(?im)^\s*Sample\s+Output\s*$", text)
    if not input_heading or not output_heading or output_heading.start() <= input_heading.end():
        raise RuntimeError("官方 PDF 中找不到 Sample Input / Sample Output；此題可能沒有標準範例格式")

    def clean_sample_section(section: str) -> str:
        lines = []
        skip_blank_after_page_noise = False
        for raw_line in section.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if "\f" in raw_line:
                raw_line = raw_line.replace("\f", "")
                skip_blank_after_page_noise = True

            line = raw_line.rstrip()
            if is_pdf_noise_line(line):
                if lines and not lines[-1].strip():
                    lines.pop()
                skip_blank_after_page_noise = True
                continue
            if not line.strip():
                if skip_blank_after_page_noise:
                    continue
                lines.append("")
                continue
            skip_blank_after_page_noise = False
            # pypdf layout 模式會為了還原視覺欄位塞入大量水平空白；
            # Sample 測資保留垂直空行，只去掉跨頁/頁首頁尾附近的空白。
            lines.append(re.sub(r"[ \t]{2,}", " ", line.strip()))

        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        return "\n".join(lines).strip()

    input_data = clean_sample_section(text[input_heading.end():output_heading.start()])
    expected_output = clean_sample_section(text[output_heading.end():])
    if not input_data or not expected_output:
        raise RuntimeError("官方 PDF 的範例輸入或輸出為空")
    return {
        "status": "FETCHED",
        "source": "sample",
        "problem_id": str(problem_number),
        "input_data": input_data[:1_000_000],
        "expected_output": expected_output[:1_000_000],
        "message": "已從 UVA 官方 PDF 載入題目範例",
    }


def fetch_udebug_test(problem_number: int) -> dict:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    def is_udebug_blocked(text: str) -> bool:
        lowered = (text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "access denied",
                "attention required",
                "sorry, you have been blocked",
                "cloudflare ray id",
                "just a moment",
                "checking if the site connection is secure",
            )
        )

    udebug_url = f"https://www.udebug.com/UVa/{problem_number}"
    user_agent = (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    try:
        preflight = requests.get(udebug_url, headers={"User-Agent": user_agent}, timeout=(5, 12))
        if preflight.status_code in {403, 429, 503} or is_udebug_blocked(preflight.text):
            raise RuntimeError(
                "uDebug 目前被 Cloudflare / 防機器人機制擋住，無法由後端自動抓測資；"
                "請先用「開啟 uDebug」手動複製，或改用自訂測資"
            )
    except requests.Timeout as exc:
        raise RuntimeError("uDebug 預檢逾時（12 秒），先停止等待；請稍後再試或手動開啟 uDebug") from exc
    except requests.RequestException:
        # 有些環境 requests 連不上但 Chromium 可開；保留 Selenium 後援。
        pass

    chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
    driver_path = shutil.which("chromedriver")
    if not chromium_path or not driver_path:
        raise RuntimeError("worker 缺少 Chromium 或 chromedriver")

    options = webdriver.ChromeOptions()
    options.binary_location = chromium_path
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"--user-agent={user_agent}")

    driver = None
    stage = "啟動 Chromium"
    try:
        driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
        driver.set_page_load_timeout(25)
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {"urls": [
                "*://*.doubleclick.net/*",
                "*://*.googlesyndication.com/*",
                "*://*.google-analytics.com/*",
                "*://*.googletagmanager.com/*",
            ]},
        )
        stage = "開啟 uDebug 題目頁"
        driver.get(udebug_url)

        page_text = driver.execute_script("return document.body?.innerText || '';")
        if is_udebug_blocked(page_text):
            raise RuntimeError(
                "uDebug 暫時阻擋自動存取（Cloudflare / 防機器人頁），請用「開啟 uDebug」手動複製或改用自訂測資"
            )

        stage = "等待公開測資清單"
        WebDriverWait(driver, 18).until(
            lambda browser: browser.execute_script(
                """
                const text = document.body?.innerText || '';
                if (/Access denied|Attention Required|Sorry, you have been blocked|Cloudflare Ray ID|Just a moment/i.test(text)) {
                    throw new Error('uDebug 被 Cloudflare / 防機器人頁阻擋');
                }
                return Boolean(document.querySelector('a.input_desc[data-id]'));
                """
            )
        )
        stage = "載入最高票輸入"
        selected = driver.execute_script(
            """
            const link = document.querySelector('a.input_desc[data-id]');
            if (!link) return null;
            const id = link.dataset.id;
            link.click();
            return id;
            """
        )
        if not selected:
            raise RuntimeError("uDebug 找不到公開測資")

        WebDriverWait(driver, 12).until(
            lambda browser: bool(
                browser.execute_script(
                    """
                    const selectors = [
                        '#edit-input-data',
                        'textarea[name="input_data"]',
                        'textarea[name*="input"]',
                        'textarea[id*="input"]'
                    ];
                    return selectors.map((selector) => document.querySelector(selector)?.value || '').find(Boolean) || '';
                    """
                )
            )
        )
        stage = "取得 Accepted Output"
        trigger_result = driver.execute_script(
            """
            const isVisible = (el) => Boolean(el && el.getClientRects().length);
            const textOf = (el) => [
                el.textContent,
                el.value,
                el.title,
                el.getAttribute('aria-label'),
                el.id,
                el.name,
            ].filter(Boolean).join(' ').trim();
            const outputValue = () => {
                const selectors = [
                    '#edit-output-data',
                    'textarea[name="output_data"]',
                    'textarea[name*="output"]',
                    'textarea[id*="output"]'
                ];
                for (const selector of selectors) {
                    const value = document.querySelector(selector)?.value || '';
                    if (value) return value;
                }
                return '';
            };
            if (outputValue()) return {ok: true, clicked: 'already-filled'};

            const dataInput = document.querySelector('#edit-data, #edit-input-data, textarea[name="input_data"], textarea[name*="input"]');
            if (dataInput) {
                dataInput.dispatchEvent(new Event('input', {bubbles: true}));
                dataInput.dispatchEvent(new Event('change', {bubbles: true}));
                dataInput.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }

            // uDebug 是 Drupal 頁面；有些題目不顯示明確 button，但 Drupal.ajax 仍掛著。
            if (window.Drupal?.ajax) {
                const ajaxItems = Object.values(window.Drupal.ajax).filter(Boolean);
                const preferredAjax = ajaxItems.find((ajax) => {
                    const haystack = [
                        ajax?.element?.id,
                        ajax?.element?.name,
                        ajax?.element?.value,
                        ajax?.element_settings?.selector,
                        ajax?.element_settings?.event,
                        ajax?.url,
                    ].filter(Boolean).join(' ');
                    return /(output|debug|submit|data)/i.test(haystack);
                });
                if (preferredAjax?.eventResponse) {
                    try {
                        const eventName = preferredAjax.element_settings?.event || 'click';
                        const event = window.jQuery ? window.jQuery.Event(eventName) : new Event(eventName, {bubbles: true, cancelable: true});
                        preferredAjax.eventResponse.call(preferredAjax, preferredAjax.element, event);
                        if (outputValue()) return {ok: true, clicked: 'Drupal.ajax'};
                    } catch (error) {
                        // 繼續嘗試一般 DOM/form submit。
                    }
                }
            }

            const candidates = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], input[type="image"], a'))
                .map((el) => ({el, label: textOf(el)}))
                .filter((item) =>
                    /(accepted\\s*output|get\\s*output|output|debug|submit|run|send)/i.test(item.label) &&
                    !/(copy|delete|add\\s*input|vote|sign\\s*up)/i.test(item.label)
                );
            const preferred = candidates.find((item) => /(accepted\\s*output|get\\s*output|debug)/i.test(item.label))
                || candidates.find((item) => /(submit|run|send)/i.test(item.label))
                || candidates[0];
            if (!preferred) {
                const form = dataInput?.closest('form') || document.querySelector('form');
                if (form) {
                    if (typeof form.requestSubmit === 'function') form.requestSubmit();
                    else form.submit();
                    return {ok: true, clicked: 'form-submit-no-button'};
                }
                return {
                    ok: false,
                    error: '找不到 uDebug 取得輸出的按鈕',
                    buttons: Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a'))
                        .filter(isVisible)
                        .slice(0, 20)
                        .map(textOf)
                };
            }
            preferred.el.scrollIntoView({block: 'center', inline: 'center'});
            preferred.el.click();
            if (outputValue()) return {ok: true, clicked: preferred.label};

            const form = dataInput?.closest('form') || document.querySelector('form');
            if (form) {
                const submitter = Array.from(form.querySelectorAll('button, input[type="submit"], input[type="button"]'))
                    .find((el) => /(output|debug|submit|run|send)/i.test(textOf(el)));
                if (typeof form.requestSubmit === 'function' && submitter) form.requestSubmit(submitter);
                else if (typeof form.requestSubmit === 'function') form.requestSubmit();
                else form.submit();
                return {ok: true, clicked: 'form-submit'};
            }
            return {ok: true, clicked: preferred.label || 'clicked'};
            """
        )
        if trigger_result and not trigger_result.get("ok"):
            buttons = " / ".join(trigger_result.get("buttons") or [])
            raise RuntimeError(f"{trigger_result.get('error')}；目前頁面按鈕：{buttons[:500]}")

        WebDriverWait(driver, 24).until(
            lambda browser: bool(
                browser.execute_script(
                    """
                    const text = document.body?.innerText || '';
                    if (/Access denied|Attention Required|Sorry, you have been blocked|Cloudflare Ray ID|Just a moment/i.test(text)) {
                        throw new Error('uDebug 被 Cloudflare / 防機器人頁阻擋');
                    }
                    const selectors = [
                        '#edit-output-data',
                        'textarea[name="output_data"]',
                        'textarea[name*="output"]',
                        'textarea[id*="output"]'
                    ];
                    return selectors.map((selector) => document.querySelector(selector)?.value || '').find(Boolean) || '';
                    """
                )
            )
        )
        data = driver.execute_script(
            """
            const readFirst = (selectors) =>
                selectors.map((selector) => document.querySelector(selector)?.value || '').find(Boolean) || '';
            return {
                input: readFirst([
                    '#edit-input-data',
                    'textarea[name="input_data"]',
                    'textarea[name*="input"]',
                    'textarea[id*="input"]'
                ]),
                output: readFirst([
                    '#edit-output-data',
                    'textarea[name="output_data"]',
                    'textarea[name*="output"]',
                    'textarea[id*="output"]'
                ]),
                title: document.querySelector('main h1')?.textContent?.trim() || ''
            };
            """
        )
        if not data["input"] or not data["output"]:
            raise RuntimeError("uDebug 回傳的輸入或正確輸出為空")
        return {
            "status": "FETCHED",
            "source": "udebug",
            "problem_id": str(problem_number),
            "input_data": data["input"][:1_000_000],
            "expected_output": data["output"][:1_000_000],
            "title": data["title"],
            "udebug_input_id": str(selected),
            "message": f"已載入 uDebug 最高票測資 #{selected}",
        }
    except TimeoutException as exc:
        url = driver.current_url if driver else "尚未開啟頁面"
        detail = ""
        if driver is not None:
            try:
                diagnostics = driver.execute_script(
                    """
                    const textOf = (el) => [
                        el.textContent,
                        el.value,
                        el.title,
                        el.getAttribute('aria-label'),
                        el.id,
                        el.name,
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    return {
                        title: document.title || '',
                        buttons: Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a'))
                            .filter((el) => el.getClientRects().length)
                            .slice(0, 18)
                            .map(textOf),
                        textareas: Array.from(document.querySelectorAll('textarea'))
                            .map((el) => `${el.id || '(no id)'}:${el.name || '(no name)'}:${(el.value || '').length}`),
                        ajax: Object.values(window.Drupal?.ajax || {})
                            .filter(Boolean)
                            .slice(0, 12)
                            .map((ajax) => [
                                ajax?.element?.id,
                                ajax?.element?.name,
                                ajax?.element?.value,
                                ajax?.element_settings?.selector,
                                ajax?.element_settings?.event,
                                ajax?.url,
                            ].filter(Boolean).join('|'))
                    };
                    """
                )
                detail = (
                    f"；title={diagnostics.get('title', '')[:80]}"
                    f"；buttons={' / '.join(diagnostics.get('buttons') or [])[:500]}"
                    f"；textareas={' / '.join(diagnostics.get('textareas') or [])[:300]}"
                    f"；ajax={' / '.join(diagnostics.get('ajax') or [])[:500]}"
                )
            except Exception:
                detail = ""
        raise RuntimeError(f"uDebug 逾時（階段：{stage}；頁面：{url}{detail}）") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"uDebug 抓取失敗（階段：{stage}）：{type(exc).__name__}: {exc}") from exc
    finally:
        if driver is not None:
            driver.quit()


@worker.task
def fetch_test_data_task(judge: str, problem_id: str, source: str) -> dict:
    try:
        normalized_judge = (judge or "uva").lower()
        if source == "sample" and normalized_judge == "uva":
            return fetch_uva_sample(validate_problem_number(problem_id))
        if source == "udebug" and normalized_judge == "uva":
            return fetch_udebug_test(validate_problem_number(problem_id))
        if source == "sample" and normalized_judge == "codeforces":
            return fetch_codeforces_sample(problem_id)
        return {"status": "SE", "error_log": "Unsupported test source"}
    except Exception as exc:
        return {"status": "SE", "error_log": f"{type(exc).__name__}: {exc}"}


@worker.task
def ask_ai_helper_task(
    mode: str,
    judge: str,
    problem_id: str,
    language: str,
    code: str,
    input_data: str,
    expected_output: str,
    stdout: str,
    error_log: str,
    verdict: str,
    question: str,
) -> dict:
    provider = os.getenv("AI_PROVIDER", AI_PROVIDER).lower()
    if provider not in {"openai", "gemini"}:
        return {
            "status": "SE",
            "message": "AI_PROVIDER 設定錯誤，請使用 openai 或 gemini。",
            "error_log": f"AI_PROVIDER={provider}",
        }

    api_key_env = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        return {
            "status": "SE",
            "message": f"尚未設定 {api_key_env}，無法使用 AI 助教。",
            "error_log": f"Set {api_key_env} in .env and restart the worker.",
        }

    problem_statement = ""
    if mode in {"translate", "tests", "expected"}:
        try:
            normalized_judge = (judge or "uva").lower()
            if normalized_judge == "codeforces":
                problem_statement = extract_codeforces_problem_statement(problem_id)
            else:
                problem_statement = extract_uva_problem_statement(validate_problem_number(problem_id))
        except Exception as exc:
            action = "題目翻譯" if mode == "translate" else "AI 助教"
            return {
                "status": "SE",
                "message": f"{action}無法讀取題目頁，請檢查題號、網站連線或稍後再試。",
                "error_log": f"{type(exc).__name__}: {exc}",
            }

    prompt = build_ai_prompt(
        mode=mode,
        judge=judge,
        problem_id=problem_id,
        language=language,
        code=code,
        input_data=input_data,
        expected_output=expected_output,
        stdout=stdout,
        error_log=error_log,
        verdict=verdict,
        question=question,
        problem_statement=problem_statement,
    )

    try:
        if provider == "openai":
            model = os.getenv("OPENAI_MODEL", OPENAI_MODEL)
            response = requests.post(
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": [
                        {
                            "role": "system",
                            "content": "You are a careful programming tutor for online judge problems. Do not reveal a full accepted solution.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_output_tokens": int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "900")),
                },
                timeout=(10, 90),
            )
        else:
            model = os.getenv("GEMINI_MODEL", GEMINI_MODEL)
            endpoint = f"{GEMINI_API_URL}/models/{model}:generateContent"
            response = requests.post(
                endpoint,
                params={"key": api_key},
                headers={"Content-Type": "application/json"},
                json={
                    "systemInstruction": {
                        "parts": [
                            {
                                "text": "You are a careful programming tutor for online judge problems. Do not reveal a full accepted solution."
                            }
                        ]
                    },
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "900")),
                        "temperature": float(os.getenv("GEMINI_TEMPERATURE", "0.4")),
                    },
                },
                timeout=(10, 90),
            )
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        if exc.response is not None:
            try:
                detail = exc.response.text[:4000]
            except Exception:
                detail = ""
        return {
            "status": "SE",
            "message": f"AI 助教呼叫 {provider} 失敗。請檢查 API key、模型名稱、帳戶額度、免費層限制或網路連線。",
            "error_log": f"{exc}\n{detail}".strip(),
        }
    except requests.RequestException as exc:
        return {
            "status": "SE",
            "message": f"AI 助教呼叫 {provider} 失敗。請檢查 worker 是否能連外。",
            "error_log": str(exc),
        }

    data = response.json()
    text = extract_openai_text(data) if provider == "openai" else extract_gemini_text(data)
    if not text:
        return {"status": "SE", "message": "AI 助教沒有回傳文字。", "error_log": str(data)[:4000]}

    return {
        "status": "AI",
        "mode": mode,
        "provider": provider,
        "model": model,
        "message": text[:20_000],
    }


class CodeforcesClient:
    BASE_URL = "https://codeforces.com"

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    @staticmethod
    def language_id(language: str) -> str:
        language = {
            "c": "cf_c11", "cpp": "cf_cpp17", "cpp17": "cf_cpp17", "cpp20": "cf_cpp20", "py": "cf_py313", "java": "cf_java21", "js": "cf_node",
        }.get(language, language)
        defaults = {
            "cf_c11": "43",
            "cf_cpp17": "54",
            "cf_cpp20": "73",
            "cf_cpp23": "89",
            "cf_java21": "87",
            "cf_py313": "100",
            "cf_pypy310": "72",
            "cf_js_v8": "34",
            "cf_node": "55",
            "cf_rust2021": "75",
            "cf_rust2024": "95",
            "cf_go": "32",
            "cf_perl": "13",
            "cf_php": "6",
            "cf_ruby": "67",
            "cf_ocaml": "19",
            "cf_d": "28",
        }
        env_names = {
            "cf_c11": "CODEFORCES_C_LANGUAGE_ID",
            "cf_cpp17": "CODEFORCES_CPP_LANGUAGE_ID",
            "cf_cpp20": "CODEFORCES_CPP20_LANGUAGE_ID",
            "cf_cpp23": "CODEFORCES_CPP23_LANGUAGE_ID",
            "cf_java21": "CODEFORCES_JAVA_LANGUAGE_ID",
            "cf_py313": "CODEFORCES_PY_LANGUAGE_ID",
            "cf_pypy310": "CODEFORCES_PYPY_LANGUAGE_ID",
            "cf_js_v8": "CODEFORCES_JS_V8_LANGUAGE_ID",
            "cf_node": "CODEFORCES_NODE_LANGUAGE_ID",
            "cf_rust2021": "CODEFORCES_RUST2021_LANGUAGE_ID",
            "cf_rust2024": "CODEFORCES_RUST2024_LANGUAGE_ID",
            "cf_go": "CODEFORCES_GO_LANGUAGE_ID",
            "cf_perl": "CODEFORCES_PERL_LANGUAGE_ID",
            "cf_php": "CODEFORCES_PHP_LANGUAGE_ID",
            "cf_ruby": "CODEFORCES_RUBY_LANGUAGE_ID",
            "cf_ocaml": "CODEFORCES_OCAML_LANGUAGE_ID",
            "cf_d": "CODEFORCES_D_LANGUAGE_ID",
        }
        return os.getenv(env_names.get(language, "CODEFORCES_CPP_LANGUAGE_ID"), defaults.get(language, "54"))


    @staticmethod
    def verdict_status(verdict: str) -> str | None:
        normalized = " ".join((verdict or "").lower().split())
        if not normalized or any(word in normalized for word in ("queue", "running", "testing", "judging", "pending")):
            return None
        mappings = (
            ("accepted", "AC"),
            ("wrong answer", "WA"),
            ("time limit", "TLE"),
            ("runtime error", "RE"),
            ("compilation error", "CE"),
            ("memory limit", "MLE"),
            ("idleness limit", "ILE"),
            ("output limit", "OLE"),
            ("presentation error", "PE"),
            ("skipped", "SKIPPED"),
            ("rejected", "REJECTED"),
        )
        return next((status for marker, status in mappings if marker in normalized), "JUDGED")

    @staticmethod
    def submission_rows(driver) -> list[dict]:
        return driver.execute_script(
            """
            return Array.from(document.querySelectorAll('tr[data-submission-id], table.status-frame-datatable tr'))
              .map((row) => {
                const cells = Array.from(row.querySelectorAll('td')).map((td) => td.innerText.trim()).filter(Boolean);
                const id = row.getAttribute('data-submission-id') || (cells[0] || '').match(/\\d+/)?.[0] || '';
                const verdictNode = row.querySelector('.submissionVerdictWrapper, span.verdict-accepted, span.verdict-rejected');
                const verdict = verdictNode ? verdictNode.innerText.trim() : (cells.find((text) => /accepted|wrong answer|time limit|runtime error|compilation error|memory limit|running|queue|testing/i.test(text)) || '');
                return {id, cells, verdict};
              })
              .filter((item) => item.id && item.cells.length >= 4);
            """
        )

    def submit(self, problem_id: str, language: str, code: str) -> dict:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait

        contest_id, index = parse_codeforces_problem_id(problem_id)
        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        driver_path = shutil.which("chromedriver")
        if not chromium_path or not driver_path:
            raise RuntimeError("worker needs Chromium and chromedriver for Codeforces submit")

        options = webdriver.ChromeOptions()
        options.binary_location = chromium_path
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1360,900")
        options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

        driver = None
        try:
            driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
            driver.set_page_load_timeout(35)
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd(
                "Network.setBlockedURLs",
                {"urls": ["*://*.googlesyndication.com/*", "*://*.doubleclick.net/*", "*://mc.yandex.ru/*"]},
            )

            driver.get(f"{self.BASE_URL}/enter?back=%%2F")
            login_result = driver.execute_script(
                """
                if (document.querySelector('a[href*="/logout"]')) return {ok: true, already: true};
                const handleSelector = [
                  'input[name="handleOrEmail"]',
                  'input[name="handle"]',
                  'input#handleOrEmail',
                  'input#handle',
                  'input[type="text"]',
                  'input[type="email"]'
                ].join(',');
                const passwordSelector = 'input[name="password"], input#password, input[type="password"]';
                let form = Array.from(document.forms).find((item) =>
                  item.querySelector(handleSelector) && item.querySelector(passwordSelector)
                );
                const handle = form ? form.querySelector(handleSelector) : document.querySelector(handleSelector);
                const password = form ? form.querySelector(passwordSelector) : document.querySelector(passwordSelector);
                if (!handle || !password) {
                  const text = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 800);
                  return {
                    ok: false,
                    error: `Cannot find Codeforces login form. url=${location.href}; title=${document.title}; text=${text}`
                  };
                }
                form = form || handle.closest('form') || password.closest('form');
                handle.focus();
                handle.value = arguments[0];
                password.focus();
                password.value = arguments[1];
                for (const element of [handle, password]) {
                  element.dispatchEvent(new Event('input', {bubbles: true}));
                  element.dispatchEvent(new Event('change', {bubbles: true}));
                }
                const submit = (form && form.querySelector('input[type="submit"], button[type="submit"]')) ||
                  document.querySelector('input[type="submit"], button[type="submit"]');
                if (form && typeof form.requestSubmit === 'function') form.requestSubmit(submit || undefined);
                else if (submit) submit.click();
                else if (form) form.submit();
                else return {ok: false, error: 'Cannot find Codeforces login submit button'};
                return {ok: true};
                """,
                self.username,
                self.password,
            )
            if not login_result.get("ok"):
                error = login_result.get("error", "Codeforces login failed")
                if "Just a moment" in error or "Cloudflare" in error or "security verification" in error:
                    raise RuntimeError("Codeforces Cloudflare security verification blocked worker automation. Use manual submit from the browser instead.")
                raise RuntimeError(error)

            try:
                WebDriverWait(driver, 25).until(
                    lambda browser: browser.execute_script(
                        "return Boolean(document.querySelector('a[href*=\"/logout\"]')) || !document.querySelector('input[name=\"password\"]');"
                    )
                )
            except TimeoutException as exc:
                raise RuntimeError("Codeforces login timed out; CAPTCHA or two-factor checks may be blocking automation") from exc

            page_text = driver.execute_script("return document.body?.innerText || '';")
            page_title = driver.execute_script("return document.title || '';")
            current_url = driver.current_url
            if "Invalid handle" in page_text or "Invalid password" in page_text or "Captcha" in page_text or "captcha" in page_text.lower():
                snippet = " ".join(page_text.split())[:800]
                raise RuntimeError(f"Codeforces login failed; check credentials, CAPTCHA, or bot protection. url={current_url}; title={page_title}; text={snippet}")

            submissions_url = f"{self.BASE_URL}/submissions/{self.username}"
            driver.get(submissions_url)
            rows = self.submission_rows(driver)
            previous_id = rows[0]["id"] if rows else None

            driver.get(f"{self.BASE_URL}/problemset/submit")
            submit_result = driver.execute_script(
                """
                const problemCode = arguments[0];
                const languageId = arguments[1];
                const source = arguments[2];
                const form = Array.from(document.forms).find((item) =>
                  item.querySelector('input[name="submittedProblemCode"], input[name="problemCode"]') &&
                  item.querySelector('select[name="programTypeId"]') &&
                  item.querySelector('textarea[name="source"]')
                );
                if (!form) return {ok: false, error: 'Cannot find Codeforces submit form'};
                const problem = form.querySelector('input[name="submittedProblemCode"], input[name="problemCode"]');
                const language = form.querySelector('select[name="programTypeId"]');
                const sourceArea = form.querySelector('textarea[name="source"]');
                problem.value = problemCode;
                language.value = languageId;
                sourceArea.value = source;
                for (const element of [problem, language, sourceArea]) {
                  element.dispatchEvent(new Event('input', {bubbles: true}));
                  element.dispatchEvent(new Event('change', {bubbles: true}));
                }
                const submit = form.querySelector('input[type="submit"], button[type="submit"]');
                if (typeof form.requestSubmit === 'function') form.requestSubmit(submit || undefined);
                else form.submit();
                return {ok: true};
                """,
                f"{contest_id}{index}",
                self.language_id(language),
                code,
            )
            if not submit_result.get("ok"):
                raise RuntimeError(submit_result.get("error", "Codeforces submit form failed"))

            time.sleep(2)
            driver.get(submissions_url)
            deadline = time.monotonic() + int(os.getenv("CODEFORCES_VERDICT_TIMEOUT", "90"))
            latest = None
            status = None
            while time.monotonic() < deadline:
                rows = self.submission_rows(driver)
                latest = next((row for row in rows if row["id"] != previous_id), rows[0] if rows else None)
                if latest:
                    status = self.verdict_status(latest.get("verdict", ""))
                    if status:
                        break
                time.sleep(3)
                driver.refresh()

            if not latest:
                raise RuntimeError("Codeforces submission was sent, but no new submission row was found")
            return {
                "status": status or "SUBMITTED",
                "problem_id": f"{contest_id}{index}",
                "submission_id": latest.get("id", ""),
                "verdict": latest.get("verdict", ""),
                "message": latest.get("verdict", "") or "Codeforces submission queued",
            }
        finally:
            if driver is not None:
                driver.quit()


@worker.task
def submit_codeforces_task(problem_id: str, language: str, code: str) -> dict:
    try:
        parse_codeforces_problem_id(problem_id)
    except ValueError as exc:
        return {"status": "SE", "error_log": str(exc)}

    username = os.getenv("CODEFORCES_USERNAME", "")
    password = os.getenv("CODEFORCES_PASSWORD", "")
    if not username or not password:
        return {"status": "SE", "error_log": "Set CODEFORCES_USERNAME and CODEFORCES_PASSWORD in .env"}
    try:
        return CodeforcesClient(username, password).submit(problem_id, language, code)
    except Exception as exc:
        return {"status": "SE", "error_log": f"{type(exc).__name__}: {exc}"}


@worker.task
def submit_uva_task(problem_id: str, language: str, code: str) -> dict:
    try:
        validate_problem_number(problem_id)
    except ValueError as exc:
        return {"status": "SE", "error_log": str(exc)}

    username = os.getenv("UVA_USERNAME", "")
    password = os.getenv("UVA_PASSWORD", "")
    if not username or not password:
        return {"status": "SE", "error_log": "尚未在 .env 設定 UVA_USERNAME 與 UVA_PASSWORD"}
    try:
        return UvaClient(username, password).submit(problem_id, language, code)
    except Exception as exc:
        return {"status": "SE", "error_log": f"{type(exc).__name__}: {exc}"}

