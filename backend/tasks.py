import os
import re
import shutil
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
# Keep a conventional Celery app alias for imports and CLI compatibility.
app = worker

SANDBOX_ROOT = Path("/sandboxes")
HOST_SANDBOX_ROOT = Path(os.getenv("HOST_SANDBOX_ROOT", "/home/ian/pi_oj/backend/sandboxes"))
JUDGE_IMAGE = os.getenv("JUDGE_IMAGE", "pi_oj-judge:latest")
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
GEMINI_API_URL = os.getenv("GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
STDOUT_CAPTURE_LIMIT = int(os.getenv("OJ_STDOUT_CAPTURE_LIMIT", "120000"))
STDERR_CAPTURE_LIMIT = int(os.getenv("OJ_STDERR_CAPTURE_LIMIT", "40000"))
DEFAULT_RUN_MEM_LIMIT = os.getenv("OJ_DEFAULT_MEM_LIMIT", "128m")
JAVA_RUN_MEM_LIMIT = os.getenv("OJ_JAVA_MEM_LIMIT", "384m")
DEFAULT_RUN_TIMEOUT = int(os.getenv("OJ_RUN_TIMEOUT", "5"))
JAVA_RUN_TIMEOUT = int(os.getenv("OJ_JAVA_RUN_TIMEOUT", "8"))
GO_RUN_TIMEOUT = int(os.getenv("OJ_GO_RUN_TIMEOUT", "8"))

RUNNER_WRAPPER = r"""#!/usr/bin/env python3
import os
import selectors
import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    stdout_limit = int(os.environ.get("OJ_STDOUT_CAPTURE_LIMIT", "120000"))
    stderr_limit = int(os.environ.get("OJ_STDERR_CAPTURE_LIMIT", "40000"))
    if "--" in args:
        sep = args.index("--")
        cmd = args[sep + 1:]
    else:
        cmd = args
    if not cmd:
        open(".stderr", "wb").write(b"Runner command is empty\n")
        open(".runner_meta", "w", encoding="utf-8").write("stdout_truncated=0\nstderr_truncated=0\n")
        return 127

    stdout = bytearray()
    stderr = bytearray()
    truncated = {"stdout": False, "stderr": False}
    try:
        process = subprocess.Popen(
            cmd,
            stdin=sys.stdin.buffer,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        open(".stdout", "wb").write(b"")
        open(".stderr", "wb").write(f"{type(exc).__name__}: {exc}\n".encode("utf-8", errors="replace"))
        open(".runner_meta", "w", encoding="utf-8").write("stdout_truncated=0\nstderr_truncated=0\n")
        return 127

    selector = selectors.DefaultSelector()
    streams = {process.stdout: ("stdout", stdout, stdout_limit), process.stderr: ("stderr", stderr, stderr_limit)}
    for stream in streams:
        selector.register(stream, selectors.EVENT_READ)

    while selector.get_map():
        for key, _ in selector.select():
            stream = key.fileobj
            chunk = os.read(stream.fileno(), 8192)
            if not chunk:
                selector.unregister(stream)
                stream.close()
                continue
            name, buffer, limit = streams[stream]
            remain = max(0, limit - len(buffer))
            if remain:
                buffer.extend(chunk[:remain])
            if len(chunk) > remain:
                truncated[name] = True

    code = process.wait()
    open(".stdout", "wb").write(stdout)
    open(".stderr", "wb").write(stderr)
    with open(".runner_meta", "w", encoding="utf-8") as meta:
        meta.write(f"stdout_truncated={int(truncated['stdout'])}\n")
        meta.write(f"stderr_truncated={int(truncated['stderr'])}\n")
    return code if isinstance(code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
"""



def normalize_output(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def normalize_output_without_blank_lines(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n") if line.strip())


def truncate_context(value: str, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n... output truncated ..."


def user_error(summary: str, exc: Exception | None = None) -> str:
    if exc is None:
        return summary
    return f"{summary}\n{type(exc).__name__}: {exc}"


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
        raise RuntimeError("Timed out while downloading UVA PDF.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to download UVA PDF: {exc}") from exc

    if not response.content.startswith(b"%PDF"):
        raise RuntimeError("UVA did not return a valid PDF.")

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
        raise ValueError("Codeforces 題號格式應類似 1843A 或 1843/A")
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


def sanitize_statement_fragment(statement, base_url: str):
    """Keep Codeforces statement readable while removing risky HTML attributes."""
    for block in statement.select("script, style, iframe, object, embed, link, meta"):
        block.decompose()

    allowed_attrs = {"class", "href", "src", "alt", "title", "colspan", "rowspan"}
    url_attrs = {"href", "src"}
    for node in statement.find_all(True):
        for attr in list(node.attrs):
            attr_lower = attr.lower()
            if attr_lower.startswith("on") or attr_lower not in allowed_attrs:
                del node.attrs[attr]
                continue

            if attr_lower in url_attrs:
                raw_value = node.get(attr) or ""
                if isinstance(raw_value, list):
                    raw_value = " ".join(str(part) for part in raw_value)
                value = str(raw_value).strip()
                if not value:
                    del node.attrs[attr]
                    continue
                if value.startswith("#"):
                    node.attrs[attr] = value
                    continue

                if attr_lower == "src" and value.lower().startswith("data:image/"):
                    node.attrs[attr] = value
                    continue

                resolved = urljoin(base_url, value)
                parsed = urlparse(resolved)
                allowed_schemes = {"http", "https", "mailto"} if attr_lower == "href" else {"http", "https"}
                if parsed.scheme.lower() not in allowed_schemes:
                    del node.attrs[attr]
                    continue
                node.attrs[attr] = resolved

            if attr_lower in {"colspan", "rowspan"}:
                if not str(node.get(attr, "")).isdigit():
                    del node.attrs[attr]

    return statement


def render_codeforces_problem_statement_html(problem_id: str) -> str:
    soup = fetch_codeforces_problem_page(problem_id)
    statement = soup.select_one(".problem-statement")
    if statement is None:
        raise RuntimeError("Cannot find Codeforces problem statement")
    statement = sanitize_statement_fragment(statement, "https://codeforces.com/")
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
        "message": "已從 Codeforces 題目頁載入範例測資。",
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
        or re.search(r"^\d+\s*[-–—]\s+.+\s+\d+\s*/\s*\d+\s*$", compact)
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
        raise RuntimeError("Cannot parse UVA problem statement from PDF.")
    return statement[:80_000]


def format_ai_conversation(conversation: list[dict] | None, limit_messages: int = 12, limit_chars: int = 20_000) -> str:
    if not conversation:
        return "(none)"
    chunks = []
    used = 0
    for item in (conversation or [])[-limit_messages:]:
        role = str((item or {}).get("role", "")).strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str((item or {}).get("content", "")).strip()
        if not content:
            continue
        remaining = max(0, limit_chars - used)
        if remaining <= 0:
            break
        clipped = truncate_context(content, min(remaining, 4_000))
        used += len(clipped)
        label = "User" if role == "user" else "Assistant"
        chunks.append(f"{label}:\n```text\n{clipped}\n```")
    return "\n\n".join(chunks) if chunks else "(none)"


def build_response_style() -> str:
    return """Response style:
- Answer in Traditional Chinese.
- Start with the most useful conclusion; then give reasons, steps, and pitfalls.
- Use clean Markdown headings and short bullets. Avoid long dense paragraphs.
- Use fenced code blocks for code or exact stdin/stdout only.
- Use KaTeX-compatible math only when math helps, such as `$O(E \\log V)$` or `$\\lfloor N/k \\rfloor$`.
- Be specific to the provided constraints, verdict, code, stdin/stdout, and examples when relevant.
- If information is missing or uncertain, say what is missing instead of guessing.
- Do not reveal a full accepted solution unless the user explicitly asks for it.
- Avoid greetings, filler, and generic encouragement."""


def build_common_context(
    judge_name: str,
    problem_id: str,
    language: str,
    code: str,
    input_data: str,
    expected_output: str,
    stdout: str,
    error_log: str,
    verdict: str,
    question: str,
) -> str:
    return f"""Online judge: {judge_name}
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
    conversation: list[dict] | None = None,
    test_count: int = 12,
    generation_id: str = "",
    previous_tests: list[str] | None = None,
) -> str:
    judge_name = "Codeforces" if (judge or "uva").lower() == "codeforces" else "UVA"
    response_style = build_response_style()
    common_context = build_common_context(judge_name, problem_id, language, code, input_data, expected_output, stdout, error_log, verdict, question)
    conversation_context = format_ai_conversation(conversation)
    try:
        requested_test_count = max(1, min(100, int(test_count or 12)))
    except (TypeError, ValueError):
        requested_test_count = 12

    previous_tests = [str(item).strip() for item in (previous_tests or []) if str(item).strip()][:5]
    previous_tests_context = "\n".join(f"{index + 1}. {truncate_context(item, 900)}" for index, item in enumerate(previous_tests))
    generation_context = generation_id.strip() or f"gen-{int(time.time() * 1000)}"

    if mode == "translate":
        return f"""You are translating an online judge problem statement for a Traditional Chinese reader.

{response_style}

Task:
- Translate the problem statement into clear Traditional Chinese.
- Preserve section names, mathematical notation, variable names, limits, input format, and output format accurately.
- Do not solve the problem and do not provide code.
- If the statement contains examples, keep them readable and distinguish input from output.
- If the user wrote an extra question, answer only if it is about understanding the statement.
- Preferred structure: title, problem story/task, input, output, constraints, examples/notes.

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

{response_style}

Task:
- Use the provided stdin exactly as-is. Do not create new input and do not silently fix invalid input.
- Compute the exact expected stdout according to the online judge problem statement.
- If multiple cases are present, calculate every case in order.
- Do not provide a full accepted solution or code.
- If the stdin is invalid for the problem, explain the issue instead of inventing output.
- If you cannot confidently compute the output, say so clearly instead of guessing.
- Put the required expected stdout block first. After that, add a short verification only when helpful.

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
        return f"""You are an Online Judge test-data designer. Use the online judge problem statement to create high-quality local tests.

{response_style}

Task:
- Generate exactly {requested_test_count} logical test cases whenever the problem format has separable cases.
- If the input format is EOF-based or terminator-based and does not have an explicit case count, still create exactly {requested_test_count} logical input records before the required EOF/terminator when the format permits it.
- If an exact count is truly impossible because one input contains a single indivisible instance, say so in the case manifest and create one strong instance instead. Do not pretend that subparts are separate cases.
- Every time this request is repeated, create substantially different values, ordering, edge combinations, and case types. Do not reuse the same stdin as earlier attempts.
- Generation ID for uniqueness: {generation_context}
- Avoid recent generated inputs listed below. Do not merely shuffle or change one number; use different categories and values.
- Generate a mix of tiny, small, medium, boundary, and trap cases; do not make every case the same size.
- Combine all cases into one stdin block that can be pasted directly into the local runner.
- Produce the exact expected stdout for that stdin.
- Follow the problem's input format exactly. If the problem uses EOF, do not invent a case count. If it requires T, set T correctly. If it needs a terminator such as `0`, `0 0`, or `#`, include it correctly.
- Prefer strong tests that can still be inspected by hand. For 50/100 cases, use mostly compact cases and include only a few larger stress cases.
- Cover sample-like cases, minimum values, boundary values, normal values, repeated values, ordering traps, formatting traps, and likely WA cases.
- If current code is provided, design at least a few tests that may expose the user's likely bug.
- Do not provide a full accepted solution or code.
- If you cannot confidently compute expected output, say so instead of inventing one.
- The first two fenced blocks must be exactly stdin and expected stdout so the frontend can parse them.
- After those two blocks, include a `case manifest:` fenced text block with exactly one numbered line per logical case you generated. This manifest is used to verify count.

Required output format. Put these fenced blocks first:
stdin:
```text
...
```
expected stdout:
```text
...
```
case manifest:
```text
1. ...
2. ...
```

After the blocks, add a short Traditional Chinese section named `測資重點` explaining what the tests check.

Recent generated test summaries to avoid:
```text
{previous_tests_context or "(none)"}
```

Online judge: {judge_name}
Problem: {problem_id}
User request:
```text
{truncate_context(question, 4_000) or f"Please generate exactly {requested_test_count} bug-catching local test cases with exact expected stdout."}
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

{response_style}

Rules:
- Answer the user's current prompt directly; do not force the hint/debug/translation templates.
- Use the previous conversation when it helps maintain continuity.
- If the user asks a follow-up like "為什麼", "那這樣呢", "這段呢", infer the referent from the previous conversation.
- The user's latest question has priority over the current problem context.
- If the user asks a general programming or algorithm question, answer only that question.
- Do not mention or analyze the current online judge problem unless the user clearly asks about the current problem, their code, a verdict, stdin/stdout, sample tests, or debugging.
- If the user asks for a template, provide a compact reusable code template and a short explanation.
- Prefer this structure when suitable: conclusion, reason, example/template, complexity, pitfalls.
- Put code templates in fenced code blocks.
- If the user asks "is this problem using algorithm X?", answer yes/no first, then explain the likely correct direction.

Previous conversation, newest context may depend on this:
{conversation_context}

Optional current problem context. Ignore this section unless it is clearly relevant:
{common_context}
"""

    if mode == "hint":
        instruction = """Help the user understand the problem and find a solution direction.
- Start with a 1 to 2 sentence problem summary.
- Identify the core observation and likely algorithm/data structure.
- Explain why the algorithm fits the constraints.
- Include complexity.
- Explain input/output or formatting traps if likely.
- Give a small implementation plan, not full code.
- Suggested structure: 題意摘要, 關鍵觀察, 解法方向, 實作步驟, 複雜度, 常見陷阱."""
    else:
        instruction = """Help the user debug the current program.
- Start with the most likely bug or missing piece.
- Prioritize verdict, stdout, expected stdout, error log, and code.
- Compare expected stdout and actual stdout when both are present.
- Reference concrete code behavior instead of vague advice.
- Give a small counterexample or targeted test when possible.
- Prefer minimal fixes before proposing a full rewrite.
- Suggested structure: 判斷結果, 錯誤原因, 反例/測資, 最小修改建議, 為什麼這樣改."""

    return f"""You are an Online Judge programming tutor.

{response_style}

{instruction}

{common_context}
"""

def _read_text_file(path: Path, limit: int) -> str:
    try:
        return path.read_bytes()[:limit].decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _read_runner_meta(path: Path) -> dict[str, bool]:
    meta = {"stdout_truncated": False, "stderr_truncated": False}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in meta:
                meta[key] = value.strip() == "1"
    except FileNotFoundError:
        pass
    return meta


def output_limit_message(meta: dict[str, bool]) -> str:
    parts = []
    if meta.get("stdout_truncated"):
        parts.append(f"stdout exceeded {STDOUT_CAPTURE_LIMIT} bytes")
    if meta.get("stderr_truncated"):
        parts.append(f"stderr exceeded {STDERR_CAPTURE_LIMIT} bytes")
    if not parts:
        return ""
    return "Output limit exceeded: " + ", ".join(parts) + "."


def run_container(
    container_dir: Path,
    host_dir: Path,
    command: list[str],
    stdin_data: str = "",
    timeout: int = 5,
    mem_limit: str = DEFAULT_RUN_MEM_LIMIT,
    tmpfs_size: str = "16m",
    env_extra: dict[str, str] | None = None,
) -> tuple[int, str, str, dict[str, bool]]:
    client = docker.from_env()
    container = None
    sock = None
    for filename in (".stdout", ".stderr", ".runner_meta"):
        (container_dir / filename).unlink(missing_ok=True)
    wrapped_command = ["python3", "/sandbox/.runner.py", "--", *command]
    try:
        container = client.containers.create(
            JUDGE_IMAGE,
            command=wrapped_command,
            working_dir="/sandbox",
            user="1001:1001",
            network_disabled=True,
            mem_limit=mem_limit,
            memswap_limit=mem_limit,
            nano_cpus=500_000_000,
            pids_limit=64,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            tmpfs={"/tmp": f"rw,noexec,nosuid,size={tmpfs_size}"},
            volumes={str(host_dir): {"bind": "/sandbox", "mode": "rw"}},
            environment={
                "OJ_STDOUT_CAPTURE_LIMIT": str(STDOUT_CAPTURE_LIMIT),
                "OJ_STDERR_CAPTURE_LIMIT": str(STDERR_CAPTURE_LIMIT),
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "GOCACHE": "/tmp/go-build",
                "GOMODCACHE": "/tmp/go-mod",
                **(env_extra or {}),
            },
            stdin_open=True,
        )
        sock = container.attach_socket(params={"stdin": 1, "stream": 1})
        container.start()
        try:
            # docker-py returns a SocketIO-like wrapper here; older versions require
            # the underlying socket for stdin, so keep the private fallback isolated.
            raw_sock = getattr(sock, "_sock", sock)
            if stdin_data:
                raw_sock.sendall(stdin_data.encode("utf-8"))
            raw_sock.shutdown(1)
        except OSError:
            # Some programs exit before stdin is fully sent. Keep the actual verdict
            # from container.wait() instead of failing the whole runner.
            pass
        result = container.wait(timeout=timeout)
        stdout = _read_text_file(container_dir / ".stdout", STDOUT_CAPTURE_LIMIT)
        stderr = _read_text_file(container_dir / ".stderr", STDERR_CAPTURE_LIMIT)
        meta = _read_runner_meta(container_dir / ".runner_meta")
        if not stdout and not stderr and not (container_dir / ".runner_meta").exists():
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")[:STDERR_CAPTURE_LIMIT]
        return int(result["StatusCode"]), stdout, stderr, meta
    except requests.exceptions.ReadTimeout:
        if container:
            container.kill()
        return 124, "", "Time limit exceeded", {"stdout_truncated": False, "stderr_truncated": False}
    except requests.exceptions.ConnectionError as exc:
        if "read timed out" not in str(exc).lower():
            raise
        if container:
            container.kill()
        return 124, "", "Time limit exceeded", {"stdout_truncated": False, "stderr_truncated": False}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if container:
            container.remove(force=True)
        client.close()


@worker.task
def judge_submission_task(submission_id: str, code: str, language: str, input_data: str, expected_output: str) -> dict:
    language_aliases = {
        "cpp": "cf_cpp17", "cpp17": "cf_cpp17", "cpp20": "cf_cpp20", "c": "cf_c11", "py": "cf_py313", "java": "cf_java21", "js": "cf_node",
        "uva_c": "c", "uva_java": "java", "uva_pascal": "pascal", "uva_cpp11": "cpp11", "uva_py3": "py",
        "exam_c_mingw": "exam_c_mingw", "exam_cpp_mingw": "exam_cpp_mingw", "exam_java8": "exam_java8", "exam_java11": "exam_java11", "exam_py396": "exam_py396",
        "cf_c11": "c", "cf_cpp17": "cpp17", "cf_cpp20": "cpp20", "cf_cpp23": "cpp23", "cf_java21": "java", "cf_py313": "py", "cf_pypy310": "pypy", "cf_js_v8": "js_v8", "cf_node": "js",
        "cf_rust2021": "rust2021", "cf_rust2024": "rust2024", "cf_go": "go", "cf_perl": "perl", "cf_php": "php", "cf_ruby": "ruby", "cf_ocaml": "ocaml", "cf_d": "d",
    }
    runner_language = language_aliases.get(language)
    unsupported_local = {}
    if not runner_language:
        label = unsupported_local.get(language, language)
        return {"submission_id": submission_id, "status": "SE", "error_log": f"本機尚未安裝 {label} 的執行器。"}
    language_specs = {
        "c": {"filename": "solution.c", "compile": ["gcc", "-std=c11", "-O2", "-pipe", "solution.c", "-lm", "-o", "solution"], "run": ["./solution"]},
        "pascal": {"filename": "solution.pas", "compile": ["fpc", "-O2", "-osolution", "solution.pas"], "run": ["./solution"]},
        "cpp11": {"filename": "solution.cpp", "compile": ["g++", "-std=c++11", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "cpp17": {"filename": "solution.cpp", "compile": ["g++", "-std=c++17", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "cpp20": {"filename": "solution.cpp", "compile": ["g++", "-std=c++20", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "cpp23": {"filename": "solution.cpp", "compile": ["g++", "-std=c++23", "-O2", "-pipe", "solution.cpp", "-o", "solution"], "run": ["./solution"]},
        "py": {"filename": "solution.py", "run": ["python3", "-I", "-B", "solution.py"]},
        "pypy": {"filename": "solution.py", "run": ["pypy3", "solution.py"]},
        "java": {"filename": "Main.java", "compile": ["javac", "Main.java"], "run": ["java", "-Xmx256m", "Main"], "mem_limit": JAVA_RUN_MEM_LIMIT, "tmpfs_size": "64m", "run_timeout": JAVA_RUN_TIMEOUT},
        "exam_c_mingw": {"filename": "main.c", "compile": ["gcc", "-std=gnu11", "-O2", "-pipe", "main.c", "-lm", "-o", "main"], "run": ["./main"], "label": "C (MinGW GCC 6.3.0 compatible: GNU11)"},
        "exam_cpp_mingw": {"filename": "main.cpp", "compile": ["g++", "-std=gnu++14", "-O2", "-pipe", "main.cpp", "-o", "main"], "run": ["./main"], "label": "C++ (MinGW GCC 6.3.0 compatible: GNU++14)"},
        "exam_java8": {"filename": "Main.java", "compile": ["javac", "--release", "8", "Main.java"], "run": ["java", "-Xmx256m", "Main"], "label": "Java (AdoptOpenJDK 8 compatible: javac --release 8)", "mem_limit": JAVA_RUN_MEM_LIMIT, "tmpfs_size": "64m", "run_timeout": JAVA_RUN_TIMEOUT},
        "exam_java11": {"filename": "Main.java", "compile": ["javac", "--release", "11", "Main.java"], "run": ["java", "-Xmx256m", "Main"], "label": "Java (AdoptOpenJDK 11 compatible: javac --release 11)", "mem_limit": JAVA_RUN_MEM_LIMIT, "tmpfs_size": "64m", "run_timeout": JAVA_RUN_TIMEOUT},
        "exam_py396": {"filename": "main.py", "run": ["python3", "-I", "-B", "main.py"], "label": "Python (Python 3.9.6 compatible runner; uses container python3)"},
        "js": {"filename": "solution.js", "run": ["node", "solution.js"]},
        "js_v8": {"filename": "solution.js", "extra_files": {"cf_v8_runner.js": "const fs = require('fs');\nconst lines = fs.readFileSync(0, 'utf8').replace(/\\r\\n/g, '\\n').split('\\n');\nlet index = 0;\nglobal.readline = () => { if (index >= lines.length) throw new Error('EOF'); return lines[index++]; };\nrequire('./solution.js');\n"}, "run": ["node", "cf_v8_runner.js"]},
        "perl": {"filename": "solution.pl", "run": ["perl", "solution.pl"]},
        "php": {"filename": "solution.php", "run": ["php", "solution.php"]},
        "ruby": {"filename": "solution.rb", "run": ["ruby", "solution.rb"]},
        "ocaml": {"filename": "solution.ml", "compile": ["ocamlopt", "-O3", "solution.ml", "-o", "solution"], "run": ["./solution"]},
        "d": {"filename": "solution.d", "compile": ["ldc2", "-O2", "solution.d", "-of=solution"], "run": ["./solution"]},
        "rust2021": {"filename": "solution.rs", "compile": ["rustc", "--edition=2021", "-O", "solution.rs", "-o", "solution"], "run": ["./solution"]},
        "rust2024": {"filename": "solution.rs", "compile": ["rustc", "--edition=2024", "-O", "solution.rs", "-o", "solution"], "run": ["./solution"]},
        "go": {"filename": "solution.go", "compile": ["go", "build", "-o", "solution", "solution.go"], "run": ["./solution"], "mem_limit": os.getenv("OJ_GO_MEM_LIMIT", "256m"), "tmpfs_size": "96m", "run_timeout": GO_RUN_TIMEOUT},
    }
    spec = language_specs.get(runner_language)
    if not spec:
        return {"submission_id": submission_id, "status": "SE", "error_log": "不支援的程式語言。"}

    container_dir = SANDBOX_ROOT / submission_id
    host_dir = HOST_SANDBOX_ROOT / submission_id
    container_dir.mkdir(mode=0o777, parents=True, exist_ok=False)
    os.chmod(container_dir, 0o777)
    started = time.monotonic()

    try:
        filename = spec["filename"]
        (container_dir / ".runner.py").write_text(RUNNER_WRAPPER, encoding="utf-8")
        os.chmod(container_dir / ".runner.py", 0o755)
        (container_dir / filename).write_text(code, encoding="utf-8")
        os.chmod(container_dir / filename, 0o644)
        for extra_name, extra_content in spec.get("extra_files", {}).items():
            (container_dir / extra_name).write_text(extra_content, encoding="utf-8")
            os.chmod(container_dir / extra_name, 0o644)

        mem_limit = spec.get("mem_limit", DEFAULT_RUN_MEM_LIMIT)
        tmpfs_size = spec.get("tmpfs_size", "16m")
        if spec.get("compile"):
            exit_code, _, compile_error, compile_meta = run_container(container_dir, host_dir, spec["compile"], timeout=30, mem_limit=mem_limit, tmpfs_size=tmpfs_size)
            limit_note = output_limit_message(compile_meta)
            if limit_note:
                compile_error = (compile_error + "\n" + limit_note).strip()
            if exit_code != 0:
                return {"submission_id": submission_id, "status": "CE", "stdout": "", "error_log": compile_error[:20_000]}
        command = spec["run"]

        exit_code, stdout, stderr, run_meta = run_container(container_dir, host_dir, command, input_data, timeout=spec.get("run_timeout", DEFAULT_RUN_TIMEOUT), mem_limit=mem_limit, tmpfs_size=tmpfs_size)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = ""
        limit_note = output_limit_message(run_meta)
        if limit_note:
            status = "OLE"
            message = limit_note
            stderr = (stderr + "\n" + limit_note).strip()
        elif exit_code == 124:
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
                message = "只有空白行不同，已視為 AC。"
            else:
                status = "WA"
        elif not expected_output:
            status = "OK"
        else:
            status = "AC"

        payload = {"submission_id": submission_id, "status": status, "cpu_time": f"{elapsed_ms}ms", "stdout": stdout[:STDOUT_CAPTURE_LIMIT], "error_log": stderr[:STDERR_CAPTURE_LIMIT], "runner": spec.get("label", runner_language)}
        if message:
            payload["message"] = message
        return payload
    except ImageNotFound:
        return {"submission_id": submission_id, "status": "SE", "error_log": f"找不到評測映像檔 {JUDGE_IMAGE}，請先建置 judge image。"}
    except ContainerError as exc:
        return {"submission_id": submission_id, "status": "SE", "error_log": user_error("本機評測容器執行失敗。", exc)}
    except Exception as exc:
        return {"submission_id": submission_id, "status": "SE", "error_log": user_error("本機評測發生系統錯誤。", exc)}
    finally:
        shutil.rmtree(container_dir, ignore_errors=True)


class UvaClient:
    BASE_URL = "https://onlinejudge.org"

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
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
            raise RuntimeError("Cannot find UVA login form.")

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

        verify = self.session.get(f"{self.BASE_URL}/", timeout=20)
        verify.raise_for_status()
        verify_soup = BeautifulSoup(verify.text, "html.parser")
        if self.login_form(verify_soup) is not None:
            self.login_with_browser(url)

    def login_with_browser(self, url: str) -> None:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import StaleElementReferenceException, UnexpectedAlertPresentException
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise RuntimeError("HTTP login failed because Selenium is not available in worker.") from exc

        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        driver_path = shutil.which("chromedriver")
        if not chromium_path:
            raise RuntimeError("worker cannot find Chromium; rebuild Dockerfile with chromium.")
        if not driver_path:
            raise RuntimeError("worker cannot find chromedriver; rebuild Dockerfile with chromium-driver.")

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
                    if (!form) return {ok: false, error: 'Cannot find UVA login form'};
                    const usernameInput = form.querySelector('[name="username"]');
                    const passwordInput = form.querySelector('[name="passwd"]');
                    const submitButton = form.querySelector('[name="Submit"], input[type="submit"], button[type="submit"]');
                    if (!submitButton) return {ok: false, error: 'Cannot find UVA login submit button'};
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
                login_result = {"ok": True}
            if login_result and not login_result.get("ok"):
                raise RuntimeError(login_result.get("error", "UVA login form submission failed."))

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
                raise RuntimeError(f"UVA browser login alert: {alert_text}")
            page_soup = BeautifulSoup(driver.page_source, "html.parser")
            if self.login_form(page_soup) is not None:
                detail = self.page_error(page_soup)
                suffix = f"; page error: {detail}" if detail else ""
                raise RuntimeError(f"UVA browser login failed{suffix}")

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
                raise RuntimeError("Chromium login did not produce usable session cookies.")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"UVA Chromium login failed: {type(exc).__name__}: {exc}") from exc
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
            raise RuntimeError("Browser session does not have a usable UVA submit form.")
        payload = self.hidden_fields(submit_form)
        language = {
            "cpp": "uva_cpp11", "cpp17": "uva_cpp11", "cpp20": "uva_cpp11", "c": "uva_c", "py": "uva_py3", "java": "uva_java",
            "exam_c_mingw": "uva_c", "exam_cpp_mingw": "uva_cpp11", "exam_java8": "uva_java", "exam_java11": "uva_java", "exam_py396": "uva_py3",
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
            raise RuntimeError("UVA HTTP submit response looks invalid; check worker log or use browser submit.")
        return {
            "status": "UNKNOWN",
            "problem_id": problem_id,
            "message": "UVA accepted the HTTP request, but the result is unknown; browser mode is recommended.",
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
            raise RuntimeError("worker cannot find Chromium or chromedriver.")

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

            # UVA rows may include a variable number of problem-title cells.
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

        stage = "start Chromium"
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

            stage = "load UVA cookies"
            # Open the base domain before adding cookies; Selenium requires this.
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
                raise RuntimeError("Login cookies are valid, but My Submissions link was not found.")

            stage = "read previous UVA submissions"
            driver.get(my_submissions_url)
            previous = newest_submission()
            previous_id = previous["id"] if previous else None

            submit_url = f"{self.BASE_URL}/index.php?option=com_onlinejudge&Itemid=25&page=submit_problem"
            stage = "load UVA submit form"
            driver.get(submit_url)
            language = {
                "cpp": "uva_cpp11", "cpp17": "uva_cpp11", "cpp20": "uva_cpp11", "c": "uva_c", "py": "uva_py3", "java": "uva_java",
                "exam_c_mingw": "uva_c", "exam_cpp_mingw": "uva_cpp11", "exam_java8": "uva_java", "exam_java11": "uva_java", "exam_py396": "uva_py3",
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
            stage = "submit UVA form"
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
                    if (!form) return {ok: false, error: 'Cannot find UVA submit form'};

                    const problem = form.elements.namedItem('localid');
                    const code = form.elements.namedItem('code');
                    problem.value = problemId;
                    code.value = sourceCode;

                    const languageFields = Array.from(form.querySelectorAll('[name="language"]'));
                    if (!languageFields.length) return {ok: false, error: 'Cannot find language field'};
                    if (languageFields[0].tagName.toLowerCase() === 'select') {
                        languageFields[0].value = languageId;
                        if (languageFields[0].value !== languageId) {
                            return {ok: false, error: `Cannot select UVA language id ${languageId}`};
                        }
                        languageFields[0].dispatchEvent(new Event('change', {bubbles: true}));
                    } else {
                        const target = languageFields.find((field) => field.value === languageId) ||
                            (languageFields.length === 1 ? languageFields[0] : null);
                        if (!target) {
                            return {ok: false, error: `Cannot select UVA language id ${languageId}; available values: ${languageFields.map((f) => f.value).join(',')}`};
                        }
                        if (target.type === 'radio' || target.type === 'checkbox') target.checked = true;
                        else target.value = languageId;
                        target.dispatchEvent(new Event('change', {bubbles: true}));
                    }

                    problem.dispatchEvent(new Event('input', {bubbles: true}));
                    code.dispatchEvent(new Event('input', {bubbles: true}));
                    const button = form.querySelector('input[type="submit"], button[type="submit"]');
                    if (!button) return {ok: false, error: 'Cannot find UVA submit button'};

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
                pass
            else:
                if form_result and not form_result.get("ok"):
                    raise RuntimeError(form_result.get("error", "UVA submit form failed."))
            try:
                WebDriverWait(driver, 15).until(lambda browser: browser.current_url != submit_page_url)
            except TimeoutException:
                pass

            try:
                alert = driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                raise RuntimeError(f"UVA submit alert: {alert_text}")
            except NoAlertPresentException:
                pass

            stage = "find UVA submission ID"
            created = None
            for _ in range(15):
                driver.get(my_submissions_url)
                current = newest_submission()
                if current and current["id"] != previous_id:
                    created = current
                    break
                time.sleep(2)

            if created is None:
                raise RuntimeError("UVA submission was sent, but no new submission row was found within 30 seconds.")

            stage = "read UVA verdict"
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
                            "message": f"UVA #{submission_id}: {current['verdict']}",
                        }
                time.sleep(2)

            return {
                "status": "SUBMITTED",
                "problem_id": problem_id,
                "uva_submission_id": submission_id,
                "verdict": created["verdict"],
                "message": f"UVA submission #{submission_id} is still judging after 60 seconds.",
            }
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"UVA browser submit failed at {stage}: {type(exc).__name__}: {exc}") from exc
        finally:
            if driver is not None:
                driver.quit()


def validate_problem_number(problem_id: str) -> int:
    try:
        problem_number = int(problem_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("UVA 題號格式錯誤") from exc
    if not (100 <= problem_number <= 1800 or 10_000 <= problem_number <= 13_400):
        raise ValueError("UVA 題號必須介於 100-1800 或 10000-13400")
    return problem_number


def fetch_uva_sample(problem_number: int) -> dict:
    layout_text, plain_text = download_uva_pdf_text(problem_number)

    def has_sample_headings(value: str) -> bool:
        return bool(
            re.search(r"(?im)^\s*Sample\s+Input\s*$", value)
            and re.search(r"(?im)^\s*Sample\s+Output\s*$", value)
        )

    text = layout_text if has_sample_headings(layout_text) else plain_text
    input_heading = re.search(r"(?im)^\s*Sample\s+Input\s*$", text)
    output_heading = re.search(r"(?im)^\s*Sample\s+Output\s*$", text)
    if not input_heading or not output_heading or output_heading.start() <= input_heading.end():
        raise RuntimeError("Cannot find Sample Input / Sample Output in the UVA PDF.")

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
            # pypdf layout output may contain table-like spacing; collapse it.
            lines.append(re.sub(r"[ \t]{2,}", " ", line.strip()))

        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        return "\n".join(lines).strip()

    input_data = clean_sample_section(text[input_heading.end():output_heading.start()])
    expected_output = clean_sample_section(text[output_heading.end():])
    if not input_data or not expected_output:
        raise RuntimeError("UVA PDF sample input/output is empty.")
    return {
        "status": "FETCHED",
        "source": "sample",
        "problem_id": str(problem_number),
        "input_data": input_data[:1_000_000],
        "expected_output": expected_output[:1_000_000],
        "message": "已從 UVA PDF 載入題目範例測資。",
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
                "uDebug blocked automated access. Open uDebug manually and copy the test data if needed."
            )
    except requests.Timeout as exc:
        raise RuntimeError("uDebug timed out after 12 seconds; try again later or open uDebug manually.") from exc
    except requests.RequestException:
        pass

    chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
    driver_path = shutil.which("chromedriver")
    if not chromium_path or not driver_path:
        raise RuntimeError("worker cannot find Chromium or chromedriver.")

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
    stage = "start Chromium"
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
        stage = "open uDebug page"
        driver.get(udebug_url)

        page_text = driver.execute_script("return document.body?.innerText || '';")
        if is_udebug_blocked(page_text):
            raise RuntimeError(
                "uDebug blocked automated access. Open uDebug manually and copy the test data if needed."
            )

        stage = "find uDebug test list"
        WebDriverWait(driver, 18).until(
            lambda browser: browser.execute_script(
                """
                const text = document.body?.innerText || '';
                if (/Access denied|Attention Required|Sorry, you have been blocked|Cloudflare Ray ID|Just a moment/i.test(text)) {
                    throw new Error('uDebug blocked automated access');
                }
                return Boolean(document.querySelector('a.input_desc[data-id]'));
                """
            )
        )
        stage = "load first uDebug input"
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
            raise RuntimeError("Cannot select the requested uDebug input/output set.")

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
        stage = "request Accepted Output"
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

            // Some uDebug pages use Drupal AJAX buttons instead of a normal form submit.
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
                        // Fall back to the normal DOM/form submit path below.
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
                    error: 'Cannot find uDebug output button',
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
            raise RuntimeError(f"{trigger_result.get('error')}; buttons={buttons[:500]}")

        WebDriverWait(driver, 24).until(
            lambda browser: bool(
                browser.execute_script(
                    """
                    const text = document.body?.innerText || '';
                    if (/Access denied|Attention Required|Sorry, you have been blocked|Cloudflare Ray ID|Just a moment/i.test(text)) {
                        throw new Error('uDebug blocked automated access');
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
            raise RuntimeError("uDebug returned empty input/output.")
        return {
            "status": "FETCHED",
            "source": "udebug",
            "problem_id": str(problem_number),
            "input_data": data["input"][:1_000_000],
            "expected_output": data["output"][:1_000_000],
            "title": data["title"],
            "udebug_input_id": str(selected),
            "message": f"已載入 uDebug 測資 #{selected}",
        }
    except TimeoutException as exc:
        url = driver.current_url if driver else "not started"
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
                    f"; title={diagnostics.get('title', '')[:80]}"
                    f"; buttons={' / '.join(diagnostics.get('buttons') or [])[:500]}"
                    f"; textareas={' / '.join(diagnostics.get('textareas') or [])[:300]}"
                    f"; ajax={' / '.join(diagnostics.get('ajax') or [])[:500]}"
                )
            except Exception:
                detail = ""
        raise RuntimeError(f"uDebug timed out at {stage}; url={url}{detail}") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"uDebug failed at {stage}: {type(exc).__name__}: {exc}") from exc
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
        return {"status": "SE", "error_log": "不支援的測資來源。"}
    except Exception as exc:
        return {"status": "SE", "error_log": user_error("測資載入失敗。", exc)}



def ai_temperature_for_mode(mode: str) -> float:
    mode = (mode or "").lower()
    env_name = {
        "tests": "AI_TESTS_TEMPERATURE",
        "expected": "AI_EXPECTED_TEMPERATURE",
        "translate": "AI_TRANSLATE_TEMPERATURE",
        "hint": "AI_HINT_TEMPERATURE",
        "debug": "AI_DEBUG_TEMPERATURE",
        "ask": "AI_ASK_TEMPERATURE",
    }.get(mode, "AI_TEMPERATURE")
    fallback = {
        "tests": "0.85",
        "expected": "0.15",
        "translate": "0.25",
        "hint": "0.45",
        "debug": "0.35",
        "ask": "0.5",
    }.get(mode, "0.4")
    try:
        return float(os.getenv(env_name, os.getenv("GEMINI_TEMPERATURE", os.getenv("OPENAI_TEMPERATURE", fallback))))
    except (TypeError, ValueError):
        return float(fallback)


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
    conversation: list[dict] | None = None,
    test_count: int = 12,
    generation_id: str = "",
    previous_tests: list[str] | None = None,
) -> dict:
    provider = os.getenv("AI_PROVIDER", AI_PROVIDER).lower()
    if provider not in {"openai", "gemini"}:
        return {
            "status": "SE",
            "message": "AI_PROVIDER 設定錯誤，必須是 openai 或 gemini。",
            "error_log": f"AI_PROVIDER={provider}",
        }

    api_key_env = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        return {
            "status": "SE",
            "message": f"尚未設定 {api_key_env}，AI 助手暫時無法使用。",
            "error_log": f"請在 .env 設定 {api_key_env}，然後重啟 worker。",
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
            action = "題目翻譯" if mode == "translate" else "AI 助手"
            return {
                "status": "SE",
                "message": f"{action}讀取題目內容時失敗。",
                "error_log": user_error("題目內容載入失敗。", exc),
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
        conversation=conversation,
        test_count=test_count,
        generation_id=generation_id,
        previous_tests=previous_tests,
    )

    temperature = ai_temperature_for_mode(mode)

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
                            "content": "You are a precise online judge programming tutor. Follow the user's requested mode, answer in Traditional Chinese, use clear Markdown, and do not reveal a full accepted solution unless explicitly requested.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_output_tokens": int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "3000")),
                    "temperature": temperature,
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
                                "text": "You are a precise online judge programming tutor. Follow the user's requested mode, answer in Traditional Chinese, use clear Markdown, and do not reveal a full accepted solution unless explicitly requested."
                            }
                        ]
                    },
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "3000")),
                        "temperature": temperature,
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
            "message": f"AI 助手呼叫 {provider} 失敗，請檢查 API key、模型、額度、帳單或網路。",
            "error_log": f"{exc}\n{detail}".strip(),
        }
    except requests.RequestException as exc:
        return {
            "status": "SE",
            "message": f"AI 助手無法連線到 {provider}，請檢查 worker 網路。",
            "error_log": user_error("AI 連線失敗。", exc),
        }

    data = response.json()
    text = extract_openai_text(data) if provider == "openai" else extract_gemini_text(data)
    if not text:
        return {"status": "SE", "message": "AI 助手回傳空白內容。", "error_log": str(data)[:4000]}

    return {
        "status": "AI",
        "mode": mode,
        "provider": provider,
        "model": model,
        "message": text[:40_000],
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
        return {"status": "SE", "error_log": "請在 .env 設定 CODEFORCES_USERNAME 和 CODEFORCES_PASSWORD。"}
    try:
        return CodeforcesClient(username, password).submit(problem_id, language, code)
    except Exception as exc:
        return {"status": "SE", "error_log": user_error("Codeforces 提交失敗。", exc)}


@worker.task
def submit_uva_task(problem_id: str, language: str, code: str) -> dict:
    try:
        validate_problem_number(problem_id)
    except ValueError as exc:
        return {"status": "SE", "error_log": str(exc)}

    username = os.getenv("UVA_USERNAME", "")
    password = os.getenv("UVA_PASSWORD", "")
    if not username or not password:
        return {"status": "SE", "error_log": "請在 .env 設定 UVA_USERNAME 和 UVA_PASSWORD。"}
    try:
        return UvaClient(username, password).submit(problem_id, language, code)
    except Exception as exc:
        return {"status": "SE", "error_log": user_error("UVA 提交失敗。", exc)}


