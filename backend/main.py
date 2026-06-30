from pathlib import Path
import re
from uuid import uuid4

import requests
from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from tasks import ask_ai_helper_task, fetch_problem_statement_html, fetch_test_data_task, judge_submission_task, submit_codeforces_task, submit_uva_task, worker

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pi OJ", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

STATEMENT_SECURITY_HEADERS = {
    "Cache-Control": "public, max-age=3600",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "img-src https: data:; "
        "font-src data:; "
        "object-src 'none'; "
        "script-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def normalize_uva_problem_id(value: str) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("UVA 題號格式錯誤") from exc
    if not (100 <= number <= 1800 or 10_000 <= number <= 13_400):
        raise ValueError("UVA 題號必須介於 100-1800 或 10000-13400")
    return str(number)


def normalize_judge(value: str) -> str:
    lowered = (value or "uva").strip().lower()
    if lowered not in {"uva", "codeforces"}:
        raise ValueError("平台必須是 UVA 或 Codeforces")
    return lowered


def normalize_codeforces_problem_id(value: str) -> str:
    text = (value or "").strip().upper().replace("-", "/")
    match = re.fullmatch(r"(\d{1,6})\s*/?\s*([A-Z][0-9A-Z]?)", text)
    if not match:
        raise ValueError("Codeforces 題號格式應類似 1843A 或 1843/A")
    return f"{match.group(1)}{match.group(2)}"


def normalize_problem_id_for_judge(judge: str, value: str) -> str:
    judge = normalize_judge(judge)
    if judge == "uva":
        return normalize_uva_problem_id(value)
    return normalize_codeforces_problem_id(value)


class RunRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    language: str
    input_data: str = Field(default="", max_length=1_000_000)
    expected_output: str = Field(default="", max_length=1_000_000)


class AiConversationMessage(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(default="", max_length=20_000)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        role = (value or "").strip().lower()
        if role not in {"user", "assistant"}:
            raise ValueError("AI 對話角色格式錯誤")
        return role


class AiHelpRequest(BaseModel):
    mode: str
    judge: str = Field(default="uva")
    problem_id: str = Field(default="", max_length=32)
    language: str
    code: str = Field(default="", max_length=100_000)
    input_data: str = Field(default="", max_length=100_000)
    expected_output: str = Field(default="", max_length=100_000)
    stdout: str = Field(default="", max_length=100_000)
    error_log: str = Field(default="", max_length=50_000)
    verdict: str = Field(default="", max_length=32)
    question: str = Field(default="", max_length=4_000)
    conversation: list[AiConversationMessage] = Field(default_factory=list, max_length=24)
    test_count: int = Field(default=12, ge=1, le=100)
    generation_id: str = Field(default="", max_length=80)
    previous_tests: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"hint", "debug", "translate", "tests", "expected", "ask"}:
            raise ValueError("AI 模式格式錯誤")
        return value

    @field_validator("judge")
    @classmethod
    def validate_judge(cls, value: str) -> str:
        return normalize_judge(value)

    def model_post_init(self, __context) -> None:
        if self.mode == "ask":
            self.problem_id = self.problem_id.strip()
            return
        self.problem_id = normalize_problem_id_for_judge(self.judge, self.problem_id)






class UvaSubmitRequest(BaseModel):
    judge: str = Field(default="uva")
    problem_id: str = Field(min_length=1, max_length=32)
    language: str
    code: str = Field(min_length=1, max_length=100_000)

    @field_validator("judge")
    @classmethod
    def validate_judge(cls, value: str) -> str:
        return normalize_judge(value)

    def model_post_init(self, __context) -> None:
        self.problem_id = normalize_problem_id_for_judge(self.judge, self.problem_id)


class TestFetchRequest(BaseModel):
    judge: str = Field(default="uva")
    problem_id: str = Field(min_length=1, max_length=32)
    source: str

    @field_validator("judge")
    @classmethod
    def validate_judge(cls, value: str) -> str:
        return normalize_judge(value)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if value not in {"sample", "udebug"}:
            raise ValueError("測資來源目前只支援題目範例或 uDebug")
        return value

    def model_post_init(self, __context) -> None:
        self.problem_id = normalize_problem_id_for_judge(self.judge, self.problem_id)


SUPPORTED_LANGUAGES = {
    "uva_c", "uva_java", "uva_pascal", "uva_cpp11", "uva_py3",
    "cf_c11", "cf_cpp17", "cf_cpp20", "cf_cpp23", "cf_java21", "cf_py313", "cf_pypy310",
    "cf_js_v8", "cf_node", "cf_rust2021", "cf_rust2024", "cf_go",
    "cf_perl", "cf_php", "cf_ruby", "cf_ocaml", "cf_d",
    "exam_c_mingw", "exam_cpp_mingw", "exam_java8", "exam_java11", "exam_py396",
    "c", "cpp", "cpp17", "cpp20", "py", "java", "js",
}


def validate_language(language: str) -> None:
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=422, detail="不支援的程式語言")

@app.get("/")
def index() -> RedirectResponse:
    # Keep the redirect explicit so static assets load through the server path.
    return RedirectResponse(url="/static/index.html")


@app.get("/favicon.ico")
def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/favicon.svg")


@app.get("/api/health")
def health() -> dict:
    return {"status": "online"}


@app.get("/api/problems/{problem_id}/pdf")
def problem_pdf(problem_id: str) -> Response:
    try:
        normalized = normalize_uva_problem_id(problem_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    number = int(normalized)
    url = f"https://onlinejudge.org/external/{number // 100}/{number}.pdf"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Pi-OJ/1.0 (+https://onlinejudge.org/)"},
            timeout=(5, 60),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"無法取得 UVA 官方 PDF：{exc}") from exc

    if len(response.content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="UVA PDF 超過 15 MB 限制")
    if not response.content.startswith(b"%PDF"):
        raise HTTPException(status_code=502, detail="UVA 回應不是有效 PDF")
    return Response(
        content=response.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="uva-{number}.pdf"',
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.get("/api/problems/{judge}/{problem_id}/statement")
def problem_statement(judge: str, problem_id: str) -> HTMLResponse:
    try:
        normalized_judge = normalize_judge(judge)
        normalized_problem_id = normalize_problem_id_for_judge(normalized_judge, problem_id)
        html = fetch_problem_statement_html(normalized_judge, normalized_problem_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"題目頁載入失敗：{exc}") from exc
    return HTMLResponse(content=html, headers=STATEMENT_SECURITY_HEADERS)


@app.post("/api/run", status_code=202)
def run_code(payload: RunRequest) -> dict:
    validate_language(payload.language)
    submission_id = uuid4().hex
    task = judge_submission_task.delay(
        submission_id=submission_id,
        code=payload.code,
        language=payload.language,
        input_data=payload.input_data,
        expected_output=payload.expected_output,
    )
    return {"submission_id": submission_id, "task_id": task.id, "status": "QUEUED"}


@app.post("/api/ai/help", status_code=202)
def ai_help(payload: AiHelpRequest) -> dict:
    validate_language(payload.language)
    task = ask_ai_helper_task.delay(
        mode=payload.mode,
        judge=payload.judge,
        problem_id=payload.problem_id,
        language=payload.language,
        code=payload.code,
        input_data=payload.input_data,
        expected_output=payload.expected_output,
        stdout=payload.stdout,
        error_log=payload.error_log,
        verdict=payload.verdict,
        question=payload.question,
        conversation=[message.model_dump() for message in payload.conversation],
        test_count=payload.test_count,
        generation_id=payload.generation_id,
        previous_tests=payload.previous_tests,
    )
    return {"task_id": task.id, "status": "QUEUED"}





@app.post("/api/uva/submit", status_code=202)
def uva_submit(payload: UvaSubmitRequest) -> dict:
    validate_language(payload.language)
    if payload.judge == "codeforces":
        task = submit_codeforces_task.delay(
            problem_id=payload.problem_id,
            language=payload.language,
            code=payload.code,
        )
    else:
        task = submit_uva_task.delay(
            problem_id=payload.problem_id,
            language=payload.language,
            code=payload.code,
        )
    return {"task_id": task.id, "status": "QUEUED"}


@app.post("/api/tests/fetch", status_code=202)
def fetch_tests(payload: TestFetchRequest) -> dict:
    task = fetch_test_data_task.delay(judge=payload.judge, problem_id=payload.problem_id, source=payload.source)
    return {"task_id": task.id, "status": "QUEUED"}


@app.get("/api/tasks/{task_id}")
def task_result(task_id: str) -> dict:
    result = AsyncResult(task_id, app=worker)
    if result.state == "PENDING":
        return {"task_id": task_id, "state": "PENDING"}
    if result.state == "FAILURE":
        return {"task_id": task_id, "state": "FAILURE", "result": {"status": "SE", "error_log": str(result.info)}}
    return {"task_id": task_id, "state": result.state, "result": result.result if result.ready() else None}


