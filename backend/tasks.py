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
    if mode == "translate":
        return f"""You are translating a UVA Online Judge problem statement for a Traditional Chinese reader.

Task:
- Translate the problem statement into clear Traditional Chinese.
- Preserve section names, mathematical notation, variable names, and limits accurately.
- Do not solve the problem and do not provide code.
- If the statement contains input/output format details, make them easy to understand.
- If the user wrote an extra question, answer only if it is about understanding the statement.

UVA problem: {problem_id}
User question:
```text
{truncate_context(question, 4_000) or "(none)"}
```

Problem statement extracted from the UVA PDF:
```text
{truncate_context(problem_statement, 70_000)}
```
"""

    if mode == "tests":
        return f"""You are an Online Judge test-data assistant. Use the UVA problem statement to create local test cases.

Task:
- Answer in Traditional Chinese.
- Create ONE combined local test input and its exact expected stdout, suitable for pasting directly into the app's stdin / expected stdout boxes.
- Prefer edge cases and bug-hunting cases. If the user asks for random tests, include small random-style cases whose outputs you can compute confidently.
- Keep the generated test small enough to inspect by hand.
- Do not provide a full accepted solution or code.
- If you cannot confidently compute expected output, say so instead of inventing one.

Required output format. Put these two fenced blocks first, before any explanation:
stdin:
```text
...
```
expected stdout:
```text
...
```

After the two blocks, add a short Traditional Chinese note explaining what the test is meant to catch.

UVA problem: {problem_id}
User request:
```text
{truncate_context(question, 4_000) or "?????????????????????????????"}
```

Problem statement extracted from the UVA PDF:
```text
{truncate_context(problem_statement, 70_000)}
```

Current code, for bug-focused tests only:
```{language}
{truncate_context(code, 20_000)}
```
"""

    if mode == "hint":
        mode_instruction = """你現在是「解題提示」模式。
- 優先說明題意、輸入輸出格式、核心觀察、可用資料結構或演算法方向。
- 不要根據目前程式逐行改錯，除非使用者問題明確要求。
- 回答格式請用：1. 題意整理 2. 關鍵想法 3. 常見陷阱 4. 下一步。"""
    else:
        mode_instruction = """你現在是「除錯協助」模式。
- 優先根據目前 verdict、stdout、expected stdout、error log、程式碼找錯。
- 請指出最可疑的 1 到 3 個錯誤位置或邏輯問題。
- 回答格式請用：1. 觀察到的差異 2. 最可能原因 3. 建議修改方向 4. 建議測資。
- 不要重講完整題解，除非那是錯誤根因。"""

    return f"""你是 Online Judge 的 AI 助教。請用繁體中文回答，重點清楚、不要冗長。

{mode_instruction}

UVA 題號：{problem_id or "未填"}
語言：{language}
目前 verdict：{verdict or "尚未執行"}
使用者問題：
```text
{truncate_context(question, 4_000) or "（未填）"}
```

規則：
- 不要直接提供完整 AC 程式碼。
- 可以指出觀念、演算法方向、邊界條件、輸入格式陷阱。
- 如果使用者問題很具體，優先回答使用者問題。
- 如果資訊不足，請列出下一步應該測什麼。

程式碼：
```{language}
{truncate_context(code)}
```

stdin：
```text
{truncate_context(input_data)}
```

標準輸出 expected stdout：
```text
{truncate_context(expected_output)}
```

目前程式輸出 stdout：
```text
{truncate_context(stdout)}
```

錯誤訊息 error log：
```text
{truncate_context(error_log, 6_000)}
```
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
    if language not in {"cpp", "py"}:
        return {"submission_id": submission_id, "status": "SE", "error_log": "不支援的語言"}

    container_dir = SANDBOX_ROOT / submission_id
    host_dir = HOST_SANDBOX_ROOT / submission_id
    # judge container 使用 uid 1001；目錄只存活於單次工作，結束後立即刪除。
    container_dir.mkdir(mode=0o777, parents=True, exist_ok=False)
    os.chmod(container_dir, 0o777)
    started = time.monotonic()

    try:
        filename = "solution.cpp" if language == "cpp" else "solution.py"
        (container_dir / filename).write_text(code, encoding="utf-8")
        os.chmod(container_dir / filename, 0o644)

        if language == "cpp":
            exit_code, _, compile_error = run_container(
                host_dir,
                ["g++", "-std=c++17", "-O2", "-pipe", "solution.cpp", "-o", "solution"],
                timeout=15,
            )
            if exit_code != 0:
                return {"submission_id": submission_id, "status": "CE", "stdout": "", "error_log": compile_error[:20_000]}
            command = ["./solution"]
        else:
            command = ["python3", "-I", "-B", "solution.py"]

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
                message = "輸出內容正確；只差空白行，已視為 AC（UVA PDF 抽樣有時會漏掉空行）"
            else:
                status = "WA"
        elif not expected_output:
            status = "OK"
        else:
            status = "AC"

        payload = {
            "submission_id": submission_id,
            "status": status,
            "cpu_time": f"{elapsed_ms}ms",
            "stdout": stdout[:100_000],
            "error_log": stderr[:20_000],
        }
        if message:
            payload["message"] = message
        return payload
    except ImageNotFound:
        return {"submission_id": submission_id, "status": "SE", "error_log": f"找不到 {JUDGE_IMAGE}，請先建立 judge image"}
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
        payload.update({
            "localid": problem_id,
            "language": os.getenv("UVA_CPP_LANGUAGE_ID", "5") if language == "cpp" else os.getenv("UVA_PY_LANGUAGE_ID", "6"),
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
            language_id = (
                os.getenv("UVA_CPP_LANGUAGE_ID", "5")
                if language == "cpp"
                else os.getenv("UVA_PY_LANGUAGE_ID", "6")
            )
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
def fetch_test_data_task(problem_id: str, source: str) -> dict:
    try:
        problem_number = validate_problem_number(problem_id)
        if source == "sample":
            return fetch_uva_sample(problem_number)
        return {"status": "SE", "error_log": "目前只支援載入 UVA 題目範例；uDebug 請使用開啟按鈕手動查看"}
    except Exception as exc:
        return {"status": "SE", "error_log": f"{type(exc).__name__}: {exc}"}


@worker.task
def ask_ai_helper_task(
    mode: str,
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
    if mode in {"translate", "tests"}:
        try:
            problem_statement = extract_uva_problem_statement(validate_problem_number(problem_id))
        except Exception as exc:
            action = "題目翻譯" if mode == "translate" else "AI 測資產生"
            return {
                "status": "SE",
                "message": f"{action}前讀取 UVA PDF 失敗，請確認題號或稍後再試。",
                "error_log": f"{type(exc).__name__}: {exc}",
            }

    prompt = build_ai_prompt(
        mode=mode,
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

