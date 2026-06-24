from pathlib import Path
from uuid import uuid4

import requests
from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from tasks import ask_ai_helper_task, fetch_test_data_task, judge_submission_task, submit_uva_task, worker

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pi OJ", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def normalize_uva_problem_id(value: str) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("UVA 題號格式錯誤") from exc
    if not (100 <= number <= 1800 or 10_000 <= number <= 13_400):
        raise ValueError("UVA 題號必須介於 100–1800 或 10000–13400")
    return str(number)


class RunRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    language: str
    input_data: str = Field(default="", max_length=1_000_000)
    expected_output: str = Field(default="", max_length=1_000_000)


class AiHelpRequest(BaseModel):
    mode: str
    problem_id: str = Field(pattern=r"^\d{1,6}$")
    language: str
    code: str = Field(default="", max_length=100_000)
    input_data: str = Field(default="", max_length=100_000)
    expected_output: str = Field(default="", max_length=100_000)
    stdout: str = Field(default="", max_length=100_000)
    error_log: str = Field(default="", max_length=50_000)
    verdict: str = Field(default="", max_length=32)
    question: str = Field(default="", max_length=4_000)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"hint", "debug", "translate", "tests"}:
            raise ValueError("mode must be hint, debug, translate, or tests")
        return value

    @field_validator("problem_id")
    @classmethod
    def validate_problem_id(cls, value: str) -> str:
        return normalize_uva_problem_id(value)





class UvaSubmitRequest(BaseModel):
    problem_id: str = Field(pattern=r"^\d{1,6}$")
    language: str
    code: str = Field(min_length=1, max_length=100_000)

    @field_validator("problem_id")
    @classmethod
    def validate_problem_id(cls, value: str) -> str:
        return normalize_uva_problem_id(value)


class TestFetchRequest(BaseModel):
    problem_id: str = Field(pattern=r"^\d{1,6}$")
    source: str

    @field_validator("problem_id")
    @classmethod
    def validate_problem_id(cls, value: str) -> str:
        return normalize_uva_problem_id(value)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if value != "sample":
            raise ValueError("source 目前只支援 sample；uDebug 請使用開啟按鈕手動查看")
        return value


def validate_language(language: str) -> None:
    if language not in {"cpp", "py"}:
        raise HTTPException(status_code=422, detail="language 必須是 cpp 或 py")


@app.get("/")
def index() -> RedirectResponse:
    # 保留相對資源路徑，讓直接開啟 index.html 與伺服器模式都能正確載入樣式。
    return RedirectResponse(url="/static/index.html")


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
        problem_id=payload.problem_id,
        language=payload.language,
        code=payload.code,
        input_data=payload.input_data,
        expected_output=payload.expected_output,
        stdout=payload.stdout,
        error_log=payload.error_log,
        verdict=payload.verdict,
        question=payload.question,
    )
    return {"task_id": task.id, "status": "QUEUED"}





@app.post("/api/uva/submit", status_code=202)
def uva_submit(payload: UvaSubmitRequest) -> dict:
    validate_language(payload.language)
    task = submit_uva_task.delay(
        problem_id=payload.problem_id,
        language=payload.language,
        code=payload.code,
    )
    return {"task_id": task.id, "status": "QUEUED"}


@app.post("/api/tests/fetch", status_code=202)
def fetch_tests(payload: TestFetchRequest) -> dict:
    task = fetch_test_data_task.delay(problem_id=payload.problem_id, source=payload.source)
    return {"task_id": task.id, "status": "QUEUED"}


@app.get("/api/tasks/{task_id}")
def task_result(task_id: str) -> dict:
    result = AsyncResult(task_id, app=worker)
    if result.state == "PENDING":
        return {"task_id": task_id, "state": "PENDING"}
    if result.state == "FAILURE":
        return {"task_id": task_id, "state": "FAILURE", "result": {"status": "SE", "error_log": str(result.info)}}
    return {"task_id": task_id, "state": result.state, "result": result.result if result.ready() else None}

