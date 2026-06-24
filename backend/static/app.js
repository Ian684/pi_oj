const $ = (id) => document.getElementById(id);
const root = document.documentElement;
let codeEditor = null;
let monacoVimApi = null;
let vimMode = null;
let currentPdfObjectUrl = null;
let lastSubmittedExpectedOutput = "";
let lastRunResult = null;
let monacoLoaderSource = "local";

const MONACO_VERSION = "0.55.1";
const MONACO_VIM_VERSION = "0.4.4";

function applyTheme(theme) {
  root.dataset.theme = theme;
  const dark = theme === "dark";
  $("themeIcon").textContent = dark ? "☼" : "☾";
  $("themeLabel").textContent = dark ? "淺色" : "深色";
  $("themeToggle").setAttribute("aria-label", dark ? "切換淺色模式" : "切換深色模式");
  if (codeEditor && window.monaco) monaco.editor.setTheme(dark ? "vs-dark" : "vs");
}

const storedTheme = localStorage.getItem("pi-oj-theme");
applyTheme(storedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

const templates = {
  cpp: `#include <iostream>\nusing namespace std;\n\nint main() {\n    ios::sync_with_stdio(false);\n    cin.tie(nullptr);\n\n    // 在這裡寫程式\n    return 0;\n}\n`,
  py: `# 在這裡寫程式\n\ndef main():\n    pass\n\nif __name__ == "__main__":\n    main()\n`,
};

const saved = JSON.parse(localStorage.getItem("pi-oj-code") || "{}");
$("code").value = saved.cpp || templates.cpp;
$("problemId").value = localStorage.getItem("pi-oj-problem-id") || "";
const savedTests = JSON.parse(localStorage.getItem("pi-oj-tests") || "{}");
let activeTestSource = "sample";

const testSourceLabels = {
  sample: ["題目範例輸入", "題目範例輸出（空白時只執行）"],
  udebug: ["uDebug 輸入（請手動貼上）", "uDebug 輸出（請手動貼上）"],
  custom: ["自訂測資輸入", "自訂標準輸出（空白時只執行）"],
  ai: ["AI 測資輸入", "AI 測資預期輸出"],
};

function getCode() {
  return codeEditor ? codeEditor.getValue() : $("code").value;
}

function setCode(value) {
  if (codeEditor) codeEditor.setValue(value);
  else $("code").value = value;
}

function saveActiveTest() {
  savedTests[activeTestSource] = {
    input: $("input").value,
    expected: $("expected").value,
  };
  localStorage.setItem("pi-oj-tests", JSON.stringify(savedTests));
}

function switchTestSource(source, saveCurrent = true) {
  if (saveCurrent) saveActiveTest();
  activeTestSource = source;
  const savedTest = savedTests[source] || {};
  $("input").value = savedTest.input || "";
  $("expected").value = savedTest.expected || "";
  $("inputLabel").textContent = testSourceLabels[source][0];
  $("expectedLabel").textContent = testSourceLabels[source][1];
  $("fetchTests").hidden = !["sample", "ai"].includes(source);
  $("fetchTests").textContent = source === "ai" ? "AI 產生" : "載入";
  $("openUdebug").hidden = source !== "udebug";
  localStorage.setItem("pi-oj-test-source", source);
}

async function fetchSelectedTests() {
  const problemId = $("problemId").value;
  const source = activeTestSource;
  if (!validUvaProblemId(problemId)) {
    toast("請先輸入合法的 UVA 題號");
    return;
  }
  if (!["sample", "ai"].includes(source)) return;

  const button = $("fetchTests");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = source === "ai" ? "AI 產生中…" : "載入中…";
  try {
    let result;
    if (source === "sample") {
      const queued = await request("/api/tests/fetch", {
        method: "POST",
        body: JSON.stringify({problem_id: problemId, source}),
      });
      result = await poll(queued.task_id);
      if (!result || result.status !== "FETCHED") {
        throw new Error(result?.error_log || "測資載入失敗");
      }
    } else {
      switchSidePanel("ai");
      setAiOutput("AI 助教正在讀取 UVA PDF，並產生可套用的 input/output 測資…");
      const aiResult = await requestAi("tests");
      showAiResult(aiResult);
      const parsed = parseAiGeneratedTest(aiResult.message || "");
      if (!parsed) {
        throw new Error("AI 回答裡找不到完整的 stdin / expected stdout 區塊");
      }
      switchSidePanel("test");
      result = {
        status: "FETCHED",
        source,
        input_data: parsed.input,
        expected_output: parsed.expected,
        message: "已套用 AI 產生的測資",
      };
    }

    savedTests[source] = {
      input: result.input_data,
      expected: result.expected_output,
    };
    localStorage.setItem("pi-oj-tests", JSON.stringify(savedTests));
    if (activeTestSource === source) {
      $("input").value = result.input_data;
      $("expected").value = result.expected_output;
    }
    toast(result.message || "測資已載入");
  } catch (error) {
    toast(error.message);
    if (source === "ai") setAiOutput(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function openProblemPdf() {
  const problemId = $("problemId").value;
  if (!validUvaProblemId(problemId)) {
    toast("請先輸入合法的 UVA 題號");
    return;
  }

  const button = $("pdfToggle");
  const pdfUrl = `/api/problems/${problemId}/pdf`;
  button.disabled = true;
  $("pdfTitle").textContent = `UVA ${problemId} 題目 PDF`;
  $("openPdf").href = pdfUrl;
  $("pdfPane").hidden = false;
  document.querySelector("main").classList.add("pdf-open");
  try {
    const response = await fetch(pdfUrl);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `PDF 載入失敗（HTTP ${response.status}）`);
    }
    const blob = await response.blob();
    if (currentPdfObjectUrl) URL.revokeObjectURL(currentPdfObjectUrl);
    currentPdfObjectUrl = URL.createObjectURL(blob);
    $("problemPdf").src = currentPdfObjectUrl;
  } catch (error) {
    closeProblemPdf();
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function closeProblemPdf() {
  $("pdfPane").hidden = true;
  document.querySelector("main").classList.remove("pdf-open");
  $("problemPdf").removeAttribute("src");
  if (currentPdfObjectUrl) {
    URL.revokeObjectURL(currentPdfObjectUrl);
    currentPdfObjectUrl = null;
  }
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 3200);
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => resolve(src);
    script.onerror = () => reject(new Error(`無法載入 ${src}`));
    document.head.appendChild(script);
  });
}

async function resourceExists(url) {
  try {
    const response = await fetch(url, {method: "HEAD", cache: "no-store"});
    return response.ok;
  } catch {
    return false;
  }
}

async function ensureMonacoLoader() {
  if (typeof window.require === "function") return "existing";
  const candidates = [
    {src: `/static/vendor/monaco/vs/loader.js?v=${MONACO_VERSION}`, source: "local"},
    {src: `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs/loader.js`, source: "cdn"},
  ];
  let lastError = null;
  for (const candidate of candidates) {
    try {
      await loadScript(candidate.src);
      if (typeof window.require === "function") {
        monacoLoaderSource = candidate.source;
        return candidate.source;
      }
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("Monaco loader 載入失敗");
}

async function initializeMonaco() {
  const useFallback = (message = "Monaco 載入失敗，已切換成簡易編輯器") => {
    document.body.classList.add("monaco-failed");
    toast(message);
  };

  try {
    await ensureMonacoLoader();
  } catch (error) {
    console.error("Monaco loader failed", error);
    useFallback();
    return;
  }

  const useCdn = monacoLoaderSource === "cdn";
  const vsPath = useCdn
    ? `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`
    : "/static/vendor/monaco/vs";
  const localVimOk = !useCdn && await resourceExists("/static/vendor/monaco-vim.js");
  const vimPath = localVimOk
    ? "/static/vendor/monaco-vim"
    : `https://cdn.jsdelivr.net/npm/monaco-vim@${MONACO_VIM_VERSION}/dist/monaco-vim.umd`;

  window.require.config({paths: {
    vs: vsPath,
    "monaco-vim": vimPath,
  }});

  window.require([
    "vs/editor/editor.main",
    "vs/basic-languages/monaco.contribution",
  ], () => {
    codeEditor = monaco.editor.create($("editor"), {
      value: $("code").value,
      language: $("language").value === "cpp" ? "cpp" : "python",
      theme: root.dataset.theme === "dark" ? "vs-dark" : "vs",
      automaticLayout: true,
      fontFamily: '"Cascadia Code", "SFMono-Regular", Consolas, monospace',
      fontSize: 14,
      lineHeight: 22,
      fontLigatures: true,
      cursorBlinking: "smooth",
      cursorSmoothCaretAnimation: "on",
      smoothScrolling: true,
      minimap: {enabled: false},
      scrollBeyondLastLine: false,
      roundedSelection: false,
      renderWhitespace: "selection",
      bracketPairColorization: {enabled: true},
      guides: {bracketPairs: true, indentation: true},
      padding: {top: 12, bottom: 12},
      tabSize: 4,
      insertSpaces: true,
    });
    document.body.classList.add("monaco-ready");
    codeEditor.onDidChangeModelContent(() => {
      saved[$("language").value] = codeEditor.getValue();
      localStorage.setItem("pi-oj-code", JSON.stringify(saved));
    });

    if (typeof window.define === "function") {
      window.define(
        "monaco-editor/esm/vs/editor/editor.api",
        ["vs/editor/editor.main"],
        (editorApi) => editorApi,
      );
    }

    window.require(["monaco-vim"], (MonacoVim) => {
      monacoVimApi = MonacoVim;
      $("vimToggle").disabled = false;
      if (localStorage.getItem("pi-oj-vim") === "true") setVimMode(true, false);
    }, (error) => {
      console.error("monaco-vim initialization failed", error);
      $("vimToggle").disabled = true;
      toast("Vim 模式載入失敗，編輯器仍可正常使用");
    });
  }, (error) => {
    console.error("Monaco initialization failed", error);
    useFallback();
  });
}

function setVimMode(enabled, persist = true) {
  if (enabled) {
    if (!codeEditor || !monacoVimApi) {
      toast("Vim 模式尚未載入");
      return;
    }
    if (!vimMode) {
      $("vimStatus").hidden = false;
      vimMode = monacoVimApi.initVimMode(codeEditor, $("vimStatus"));
      codeEditor.focus();
    }
  } else if (vimMode) {
    vimMode.dispose();
    vimMode = null;
    $("vimStatus").hidden = true;
    $("vimStatus").textContent = "";
    codeEditor.focus();
  }

  const active = Boolean(vimMode);
  $("vimToggle").textContent = active ? "Vim ON" : "Vim OFF";
  $("vimToggle").setAttribute("aria-pressed", String(active));
  if (persist) localStorage.setItem("pi-oj-vim", String(active));
}

function clearDiagnostics() {
  if (codeEditor && window.monaco) {
    monaco.editor.setModelMarkers(codeEditor.getModel(), "compiler", []);
  }
}

function applyDiagnostics(result) {
  if (!codeEditor || !window.monaco || !result.error_log) return;
  const model = codeEditor.getModel();
  const markers = [];
  const log = result.error_log.replaceAll("\r\n", "\n");

  const cppPattern = /(?:^|\n).*?solution\.cpp:(\d+):(\d+):\s*(warning|error|fatal error):\s*([^\n]+)/g;
  for (const match of log.matchAll(cppPattern)) {
    const line = Math.min(Number(match[1]), model.getLineCount());
    const column = Math.min(Number(match[2]), model.getLineMaxColumn(line));
    markers.push({
      startLineNumber: line,
      startColumn: column,
      endLineNumber: line,
      endColumn: model.getLineMaxColumn(line),
      message: match[4].trim(),
      severity: match[3] === "warning" ? monaco.MarkerSeverity.Warning : monaco.MarkerSeverity.Error,
    });
  }

  if (!markers.length && $("language").value === "py") {
    const lineMatch = log.match(/File ["'][^"']*solution\.py["'], line (\d+)/);
    const errorMatch = log.match(/(SyntaxError|IndentationError|TabError):\s*([^\n]+)/);
    if (lineMatch && errorMatch) {
      const line = Math.min(Number(lineMatch[1]), model.getLineCount());
      markers.push({
        startLineNumber: line,
        startColumn: 1,
        endLineNumber: line,
        endColumn: model.getLineMaxColumn(line),
        message: `${errorMatch[1]}: ${errorMatch[2].trim()}`,
        severity: monaco.MarkerSeverity.Error,
      });
    }
  }

  monaco.editor.setModelMarkers(model, "compiler", markers);
  if (markers.length) codeEditor.revealLineInCenter(markers[0].startLineNumber);
}

function setVerdict(status, pending = false) {
  const el = $("verdict");
  el.textContent = status;
  el.className = `pill ${pending ? "neutral" : ["AC", "OK", "SUBMITTED", "AI"].includes(status) ? "ok" : "bad"}`;
}

async function request(url, options = {}) {
  const response = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

async function poll(taskId) {
  for (let i = 0; i < 360; i++) {
    const task = await request(`/api/tasks/${taskId}`);
    if (["SUCCESS", "FAILURE"].includes(task.state)) return task.result;
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  throw new Error("等待結果逾時");
}

function validUvaProblemId(value) {
  if (!/^\d+$/.test(value)) return false;
  const number = Number(value);
  return (number >= 100 && number <= 1800) || (number >= 10000 && number <= 13400);
}

function showResult(result) {
  lastRunResult = result;
  $("output").classList.remove("ai-output");
  setVerdict(result.status || "SE");
  const isUva = Boolean(result.uva_submission_id);
  $("resultMeta").hidden = false;
  const localSourceNames = {sample: "題目範例", udebug: "uDebug", ai: "AI 測資", custom: "自訂測資"};
  $("resultSource").textContent = isUva
    ? `UVA ${result.problem_id || ""}`.trim()
    : `本機 — ${localSourceNames[activeTestSource]}`;
  $("resultId").textContent = result.uva_submission_id || result.submission_id || "—";
  $("resultVerdict").textContent = result.verdict || result.status || "—";
  $("resultTime").textContent = result.cpu_time || "—";
  $("output").classList.remove("diff-output");
  const expectedForDiff = lastSubmittedExpectedOutput || $("expected").value;
  if (!isUva && result.status === "WA" && expectedForDiff) {
    renderWrongAnswerDiff(result.stdout || "", expectedForDiff, result.message);
    applyDiagnostics(result);
    return;
  }
  const parts = [];
  if (result.message) parts.push(result.message);
  if (result.stdout) parts.push(`stdout:\n${result.stdout}`);
  if (result.error_log) parts.push(`error:\n${result.error_log}`);
  $("output").textContent = parts.join("\n\n") || JSON.stringify(result, null, 2);
  applyDiagnostics(result);
}

function setAiOutput(message, isError = false) {
  const target = $("aiOutput");
  target.textContent = message;
  target.classList.toggle("ai-error", isError);
}

function parseAiGeneratedTest(message) {
  const inputMatch = message.match(/(?:^|\n)\s*(?:stdin|input)\s*:\s*```(?:text)?\s*\n([\s\S]*?)```/i);
  const expectedMatch = message.match(/(?:^|\n)\s*(?:expected\s+stdout|expected\s+output|stdout|output)\s*:\s*```(?:text)?\s*\n([\s\S]*?)```/i);
  if (!inputMatch || !expectedMatch) return null;
  return {
    input: inputMatch[1].replace(/\n$/, ""),
    expected: expectedMatch[1].replace(/\n$/, ""),
  };
}

async function requestAi(mode) {
  const result = lastRunResult || {};
  const queued = await request("/api/ai/help", {method: "POST", body: JSON.stringify({
    mode,
    problem_id: $("problemId").value,
    language: $("language").value,
    code: getCode(),
    input_data: $("input").value,
    expected_output: $("expected").value,
    stdout: result.stdout || "",
    error_log: result.error_log || "",
    verdict: result.verdict || result.status || "",
    question: $("aiQuestion").value,
  })});
  return poll(queued.task_id);
}

function showAiResult(result) {
  const aiModeNames = {hint: "AI 提示", debug: "AI 除錯", translate: "AI 翻譯", tests: "AI 測資"};
  const title = aiModeNames[result.mode] || "AI";
  const meta = [result.provider, result.model].filter(Boolean).join(" / ");
  const parts = [`${title}${meta ? ` (${meta})` : ""}`];
  if (result.message) parts.push(result.message);
  if (result.error_log) parts.push(`error:\n${result.error_log}`);
  setAiOutput(parts.join("\n\n") || JSON.stringify(result, null, 2), result.status === "SE");
}

async function askAi(mode) {
  if (!validUvaProblemId($("problemId").value)) {
    toast("請先輸入合法的 UVA 題號，AI 助教需要知道是哪一題");
    return;
  }
  const buttons = {hint: $("aiHint"), debug: $("aiDebug"), translate: $("aiTranslate")};
  const button = buttons[mode] || $("aiHint");
  button.disabled = true;
  const waitingMessages = {
    translate: "AI 助教正在讀取 UVA PDF 並翻譯題目…",
    hint: "AI 助教正在閱讀題號與程式碼…",
    debug: "AI 助教正在閱讀測試結果與程式碼…",
  };
  setAiOutput(waitingMessages[mode] || "AI 助教正在思考…");

  try {
    switchSidePanel("ai");
    showAiResult(await requestAi(mode));
  } catch (error) {
    setAiOutput(error.message, true);
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function normalizeDiffText(value) {
  return value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").replace(/\s+$/, "");
}

function renderWrongAnswerDiff(stdout, expectedOutput, message = "") {
  const output = $("output");
  output.textContent = "";
  output.classList.remove("ai-output");
  output.classList.add("diff-output");

  if (message) {
    const note = document.createElement("div");
    note.className = "diff-note";
    note.textContent = message;
    output.appendChild(note);
  }

  const title = document.createElement("div");
  title.className = "diff-title";
  title.textContent = "Wrong Answer — standard output differs from your output";
  output.appendChild(title);

  const expectedLines = normalizeDiffText(expectedOutput).split("\n");
  const actualLines = normalizeDiffText(stdout).split("\n");
  const maxLines = Math.max(expectedLines.length, actualLines.length);

  const header = document.createElement("div");
  header.className = "diff-row diff-header";
  ["Line", "Expected", "Actual"].forEach((text) => {
    const cell = document.createElement("span");
    cell.textContent = text;
    header.appendChild(cell);
  });
  output.appendChild(header);

  for (let index = 0; index < maxLines; index++) {
    const expected = expectedLines[index];
    const actual = actualLines[index];
    const row = document.createElement("div");
    row.className = "diff-row";
    if (expected !== actual) row.classList.add("diff-mismatch");

    const lineCell = document.createElement("span");
    lineCell.className = "diff-line";
    lineCell.textContent = String(index + 1);

    const expectedCell = document.createElement("span");
    expectedCell.textContent = expected === undefined ? "∅" : expected;

    const actualCell = document.createElement("span");
    actualCell.textContent = actual === undefined ? "∅" : actual;

    row.append(lineCell, expectedCell, actualCell);
    output.appendChild(row);
  }
}

async function execute(kind) {
  const button = kind === "run" ? $("run") : $("uva");
  button.disabled = true;
  clearDiagnostics();
  $("resultMeta").hidden = true;
  setVerdict(kind === "run" ? "執行中" : "提交中", true);
  $("output").classList.remove("ai-output", "diff-output");
  $("output").textContent = "工作已進入佇列…";
  try {
    let response;
    if (kind === "run") {
      lastSubmittedExpectedOutput = $("expected").value;
      response = await request("/api/run", {method: "POST", body: JSON.stringify({
        code: getCode(),
        language: $("language").value,
        input_data: $("input").value,
        expected_output: $("expected").value,
      })});
    } else {
      if (!validUvaProblemId($("problemId").value)) {
        throw new Error("UVA 題號範圍需為 100–1800 或 10000–13400");
      }
      if (!confirm(`確定要提交目前程式碼到 UVA ${$("problemId").value}？`)) return;
      response = await request("/api/uva/submit", {method: "POST", body: JSON.stringify({
        problem_id: $("problemId").value,
        language: $("language").value,
        code: getCode(),
      })});
    }
    showResult(await poll(response.task_id));
  } catch (error) {
    setVerdict("ERROR");
    $("output").classList.remove("ai-output", "diff-output");
    $("output").textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function switchSidePanel(panel) {
  const isAi = panel === "ai";
  $("testTab").classList.toggle("active", !isAi);
  $("aiTab").classList.toggle("active", isAi);
  $("testTab").setAttribute("aria-selected", String(!isAi));
  $("aiTab").setAttribute("aria-selected", String(isAi));
  $("testPanel").hidden = isAi;
  $("aiPanel").hidden = !isAi;
  localStorage.setItem("pi-oj-side-panel", panel);
}

$("testTab").addEventListener("click", () => switchSidePanel("test"));
$("aiTab").addEventListener("click", () => switchSidePanel("ai"));
$("language").addEventListener("change", (event) => {
  const previous = event.target.value === "cpp" ? "py" : "cpp";
  saved[previous] = getCode();
  setCode(saved[event.target.value] || templates[event.target.value]);
  if (codeEditor && window.monaco) {
    monaco.editor.setModelLanguage(codeEditor.getModel(), event.target.value === "cpp" ? "cpp" : "python");
  }
  clearDiagnostics();
});
$("testSource").addEventListener("change", (event) => switchTestSource(event.target.value));
$("problemId").addEventListener("input", (event) => {
  localStorage.setItem("pi-oj-problem-id", event.target.value.trim());
});
$("fetchTests").addEventListener("click", fetchSelectedTests);
$("input").addEventListener("input", saveActiveTest);
$("expected").addEventListener("input", saveActiveTest);
$("openUdebug").addEventListener("click", () => {
  const problemId = $("problemId").value;
  if (!validUvaProblemId(problemId)) {
    toast("請先輸入合法的 UVA 題號");
    return;
  }
  window.open(`https://www.udebug.com/UVa/${problemId}`, "_blank", "noopener,noreferrer");
});
$("code").addEventListener("input", () => {
  saved[$("language").value] = $("code").value;
  localStorage.setItem("pi-oj-code", JSON.stringify(saved));
});
$("code").addEventListener("keydown", (event) => {
  if (event.key === "Tab") {
    event.preventDefault();
    const start = event.target.selectionStart;
    event.target.setRangeText("    ", start, event.target.selectionEnd, "end");
  }
});
$("run").addEventListener("click", () => execute("run"));
$("aiHint").addEventListener("click", () => askAi("hint"));
$("aiDebug").addEventListener("click", () => askAi("debug"));
$("aiTranslate").addEventListener("click", () => askAi("translate"));
$("uva").addEventListener("click", () => execute("uva"));
$("vimToggle").addEventListener("click", () => setVimMode(!vimMode));
$("pdfToggle").addEventListener("click", openProblemPdf);
$("closePdf").addEventListener("click", closeProblemPdf);
$("themeToggle").addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  localStorage.setItem("pi-oj-theme", next);
});

initializeMonaco();
const initialTestSource = localStorage.getItem("pi-oj-test-source") || "sample";
$("testSource").value = testSourceLabels[initialTestSource] ? initialTestSource : "sample";
switchTestSource($("testSource").value, false);
switchSidePanel(localStorage.getItem("pi-oj-side-panel") === "ai" ? "ai" : "test");

fetch("/api/health").then((r) => r.json()).then(() => {
  $("health").textContent = "Online";
  $("health").classList.add("ok");
}).catch(() => { $("health").textContent = "Offline"; });

