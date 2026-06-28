const $ = (id) => document.getElementById(id);

const STORAGE_KEY = "pi-oj-cpe-exam";
const HISTORY_KEY = "pi-oj-cpe-exam-history";
const STATE_VERSION = 2;
const DURATION_MS = 3 * 60 * 60 * 1000;
const MONACO_VERSION = "0.55.1";
const MONACO_VIM_VERSION = "0.4.4";

const levelLabels = {must: "必考", star1: "★", star2: "★★", star3: "★★★", star4: "★★★★", star5: "★★★★★"};
const drawPlan = [["must", 1], ["star1", 2], ["star2", 2], ["star3", 1], ["star4", 1]];
const popoverConfig = {
  setup: {id: "popoverSetup", width: 500},
  problems: {id: "popoverProblems", width: 650},
  summary: {id: "popoverSummary", width: 480},
  last: {id: "popoverLast", width: 500},
  history: {id: "popoverHistory", width: 590},
};

let codeEditor = null;
let monacoVimApi = null;
let vimMode = null;
let monacoLoaderSource = "local";
let activeIndex = 0;
let toastTimer = null;
let activePopover = null;
let state = loadState();

const languageOptions = {
  exam: [
    ["exam_c_mingw", "C (MinGW GCC 6.3.0)"],
    ["exam_cpp_mingw", "C++ (MinGW GCC 6.3.0)"],
    ["exam_java8", "Java (AdoptOpenJDK 8)"],
    ["exam_java11", "Java (AdoptOpenJDK 11)"],
    ["exam_py396", "Python (Python 3.9.6)"],
  ],
  // 簡易編譯器使用原本考試編譯器的語言清單，但改用 textarea，不載入 Monaco。
  simple: [
    ["exam_c_mingw", "C (MinGW GCC 6.3.0)"],
    ["exam_cpp_mingw", "C++ (MinGW GCC 6.3.0)"],
    ["exam_java8", "Java (AdoptOpenJDK 8)"],
    ["exam_java11", "Java (AdoptOpenJDK 11)"],
    ["exam_py396", "Python (Python 3.9.6)"],
  ],
  oj: [
    ["uva_c", "ANSI C 5.3.0"],
    ["uva_java", "JAVA 1.8.0"],
    ["uva_pascal", "PASCAL 3.0.0"],
    ["uva_cpp11", "C++11 5.3.0"],
    ["uva_py3", "PYTH3 3.5.1"],
  ],
};

const monacoModes = {
  exam_c_mingw: "c",
  exam_cpp_mingw: "cpp",
  exam_java8: "java",
  exam_java11: "java",
  exam_py396: "python",
  uva_c: "c",
  uva_java: "java",
  uva_pascal: "pascal",
  uva_cpp11: "cpp",
  uva_py3: "python",
};

const templates = {
  exam_c_mingw: `#include <stdio.h>

int main(void) {
    /* Write code here */
    return 0;
}`,
  exam_cpp_mingw: `#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Write code here
    return 0;
}`,
  exam_java8: `import java.io.*;
import java.util.*;

public class Main {
    public static void main(String[] args) throws Exception {
        // Write code here
    }
}`,
  exam_java11: `import java.io.*;
import java.util.*;

public class Main {
    public static void main(String[] args) throws Exception {
        // Write code here
    }
}`,
  exam_py396: `def main():
    # Write code here
    pass

if __name__ == "__main__":
    main()`,
  uva_c: `#include <stdio.h>

int main(void) {
    // Write code here
    return 0;
}`,
  uva_java: `import java.io.*;
import java.util.*;

public class Main {
    public static void main(String[] args) throws Exception {
        // Write code here
    }
}`,
  uva_pascal: `program Main;
begin
  { Write code here }
end.`,
  uva_cpp11: `#include <iostream>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Write code here
    return 0;
}`,
  uva_py3: `def main():
    # Write code here
    pass

if __name__ == "__main__":
    main()`,
};

const testSourceLabels = {
  sample: ["題目輸入", "題目輸出（空白時只執行）"],
  udebug: ["uDebug 輸入", "uDebug 輸出"],
  custom: ["自訂測資輸入", "自訂測資輸出（空白時只執行）"],
  ai: ["AI 測資輸入", "AI 測資預期輸出"],
};

function defaultState() {
  return {
    stateVersion: STATE_VERSION,
    mode: "standard",
    examId: null,
    compilerMode: "exam",
    language: "exam_cpp_mingw",
    languageByCompilerMode: {exam: "exam_cpp_mingw", simple: "exam_cpp_mingw", oj: "uva_c"},
    startedAt: null,
    endedAt: null,
    pausedAt: null,
    pausedTotalMs: 0,
    problems: [],
    codeByProblem: {},
    inputByProblem: {},
    expectedByProblem: {},
    aiByProblem: {},
    records: {},
    submitCounts: {},
    activeIndex: 0,
    testSource: "sample",
    activeToolTab: "test",
    permissions: {aiHelper: false, aiTests: true, udebug: true, vim: false},
  };
}

function permissionsForMode(mode) {
  if (mode === "strict") return {aiHelper: false, aiTests: false, udebug: false, vim: false};
  if (mode === "standard") return {aiHelper: false, aiTests: true, udebug: true, vim: false};
  if (mode === "practice") return {aiHelper: true, aiTests: true, udebug: true, vim: true};
  return null;
}

function loadState() {
  try {
    const defaults = defaultState();
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    const storedVersion = Number(parsed.stateVersion || 0);
    const hasActiveExam = Boolean(parsed.startedAt && !parsed.endedAt);
    const stateFromStorage = {
      ...defaults,
      ...parsed,
      pausedTotalMs: Number(parsed.pausedTotalMs || 0),
      languageByCompilerMode: {...defaults.languageByCompilerMode, ...(parsed.languageByCompilerMode || {})},
      permissions: {...defaults.permissions, ...(parsed.permissions || {})},
      stateVersion: STATE_VERSION,
    };

    // 舊版 localStorage 可能記住「嚴格模式」，導致改了預設值後畫面仍停在嚴格。
    // 沒有正在進行中的考試時，將舊狀態遷移為標準模式。進行中的考試則保留原設定，避免影響已開始的紀錄。
    if (storedVersion < STATE_VERSION && !hasActiveExam) {
      stateFromStorage.mode = "standard";
      stateFromStorage.permissions = permissionsForMode("standard");
    }

    return stateFromStorage;
  } catch {
    return defaultState();
  }
}

function saveState() {
  state.activeIndex = activeIndex;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function loadHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveHistory(history) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
}

function newExamId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `exam-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("show"), 2600);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function ensurePdfUi() {
  if (!$("pdfTab")) {
    const button = document.createElement("button");
    button.id = "pdfTab";
    button.className = "side-tab";
    button.type = "button";
    button.role = "tab";
    button.setAttribute("aria-selected", "false");
    button.setAttribute("aria-controls", "pdfPanel");
    button.textContent = "題目 PDF";
    $("aiTab").before(button);
  }
  if (!$("pdfPanel")) {
    const panel = document.createElement("section");
    panel.id = "pdfPanel";
    panel.className = "side-panel pdf-panel";
    panel.role = "tabpanel";
    panel.setAttribute("aria-labelledby", "pdfTab");
    panel.hidden = true;
    panel.innerHTML = `
      <div class="pdf-side-toolbar">
        <strong id="sidePdfTitle">題目 PDF</strong>
        <a id="openSidePdf" href="#" target="_blank" rel="noopener noreferrer">新視窗開啟</a>
        <button id="expandPdf" type="button">放大閱讀</button>
      </div>
      <iframe id="sideProblemPdf" title="題目 PDF"></iframe>
    `;
    $("aiPanel").after(panel);
  }
}

function ensureTestUi() {
  const titles = document.querySelectorAll("#testPanel .io-panel .panel-title");
  if (titles[0]) titles[0].id = "inputLabel";
  if (titles[1]) titles[1].id = "expectedLabel";
  $("aiAsk").classList.add("primary");
  $("submitUva").classList.remove("primary");
}

function currentTestSource() {
  const selected = $("testSource")?.value || state.testSource || "sample";
  return testSourceLabels[selected] ? selected : "sample";
}

function testBufferKey(problemKey, source = currentTestSource()) {
  return problemKey ? `${problemKey}::${source}` : "";
}

function readTestBuffer(map, problemKey, source = currentTestSource()) {
  if (!problemKey || !map) return "";
  const key = testBufferKey(problemKey, source);
  if (Object.prototype.hasOwnProperty.call(map, key)) return map[key] || "";
  // Backward compatibility for records saved before test-source-specific buffers existed.
  // Only show the old value in sample mode, so changing source still starts clean.
  if (source === "sample" && Object.prototype.hasOwnProperty.call(map, problemKey)) return map[problemKey] || "";
  return "";
}

function loadCurrentTestBuffers() {
  const problemKey = currentProblemKey();
  if (!problemKey) {
    $("input").value = "";
    $("expected").value = "";
    return;
  }
  const source = currentTestSource();
  $("input").value = readTestBuffer(state.inputByProblem, problemKey, source);
  $("expected").value = readTestBuffer(state.expectedByProblem, problemKey, source);
}

function clearCurrentTestBuffersForSource(source = currentTestSource()) {
  const problemKey = currentProblemKey();
  $("input").value = "";
  $("expected").value = "";
  if (!problemKey) return;
  const key = testBufferKey(problemKey, source);
  if (state.inputByProblem) delete state.inputByProblem[key];
  if (state.expectedByProblem) delete state.expectedByProblem[key];
}

function switchTestSource(source, saveCurrent = true) {
  const previousSource = testSourceLabels[state.testSource] ? state.testSource : currentTestSource();
  const nextSource = testSourceLabels[source] ? source : "sample";

  // 注意：change 事件觸發時，select 的 value 已經變成新來源。
  // 如果這裡直接呼叫 saveCurrentBuffers()，會把舊畫面的 input/output 存到新來源底下，
  // 造成「題目範例污染自訂測資」或「切回來源後內容不見」。
  // 所以切換來源前，必須指定用 previousSource 保存目前畫面。
  if (saveCurrent) saveCurrentBuffers(previousSource);

  $("testSource").value = nextSource;
  state.testSource = nextSource;
  updateTestSourceUi();

  // 每一種測資來源各自保留自己的輸入/輸出。
  // 切換來源時只載入該來源已保存的內容；沒保存過就顯示空白。
  loadCurrentTestBuffers();

  saveState();
}

function updateTestSourceUi() {
  const source = $("testSource").value;
  const labels = testSourceLabels[source] || testSourceLabels.sample;
  $("inputLabel").textContent = labels[0];
  $("expectedLabel").textContent = labels[1];
  $("loadTests").hidden = !["sample", "ai"].includes(source);
  $("loadTests").textContent = source === "ai" ? "AI 產生" : "載入";
  $("openUdebug").hidden = source !== "udebug";
  $("openUdebug").textContent = "開啟 uDebug";
  $("aiExpected").hidden = source !== "custom";
  $("aiExpected").textContent = "AI 算輸出";
}

function sanitizeAiHtml(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  const allowedTags = new Set(["P", "BR", "STRONG", "EM", "CODE", "PRE", "UL", "OL", "LI", "BLOCKQUOTE", "H1", "H2", "H3", "H4", "HR", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "A"]);
  const allowedLinkProtocols = new Set(["http:", "https:", "mailto:"]);
  const remove = [];
  for (const node of template.content.querySelectorAll("*")) {
    if (!allowedTags.has(node.tagName)) {
      remove.push(node);
      continue;
    }
    for (const attr of [...node.attributes]) {
      if (node.tagName === "A" && attr.name === "href") continue;
      node.removeAttribute(attr.name);
    }
    if (node.tagName === "A") {
      const href = node.getAttribute("href") || "";
      const trimmedHref = href.trim();
      let safeHref = false;
      if (trimmedHref.startsWith("#")) {
        safeHref = true;
      } else {
        try {
          const parsed = new URL(trimmedHref, window.location.origin);
          safeHref = allowedLinkProtocols.has(parsed.protocol);
        } catch {
          safeHref = false;
        }
      }
      if (!safeHref) {
        node.removeAttribute("href");
      } else if (!trimmedHref.startsWith("#")) {
        node.target = "_blank";
        node.rel = "noopener noreferrer";
      }
    }
  }
  for (const node of remove) node.replaceWith(document.createTextNode(node.textContent || ""));
  return template.innerHTML;
}

function renderAiReadable(message, isError = false) {
  const target = $("aiOutput");
  const text = message || "";
  target.dataset.raw = text;
  target.textContent = "";
  target.classList.toggle("ai-error", isError);
  target.classList.toggle("ai-readable", !isError);
  if (!text) {
    const empty = document.createElement("div");
    empty.className = "ai-empty";
    empty.textContent = "AI 回答會顯示在這裡。";
    target.appendChild(empty);
    return;
  }
  if (isError || !window.marked) {
    const pre = document.createElement("pre");
    pre.textContent = text;
    target.appendChild(pre);
    return;
  }
  const html = marked.parse(String(text).replaceAll("\r\n", "\n").replaceAll("\r", "\n"), {
    async: false,
    breaks: true,
    gfm: true,
  });
  target.innerHTML = sanitizeAiHtml(html);
}

function setAiOutput(message, isError = false) {
  renderAiReadable(message, isError);
}

function isRunning() { return Boolean(state.startedAt && !state.endedAt && !state.pausedAt); }
function isPaused() { return Boolean(state.startedAt && !state.endedAt && state.pausedAt); }
function isEnded() { return Boolean(state.endedAt); }
function isStarted() { return Boolean(state.startedAt); }

function elapsedMs(targetState = state, now = Date.now()) {
  if (!targetState.startedAt) return 0;
  const end = targetState.endedAt || targetState.pausedAt || now;
  return Math.max(0, end - targetState.startedAt - Number(targetState.pausedTotalMs || 0));
}

function blockExamShortcuts(event) {
  const key = event.key.toLowerCase();
  const blocked = key === "f5"
    || key === "f12"
    || ((event.ctrlKey || event.metaKey) && ["r", "u"].includes(key))
    || ((event.ctrlKey || event.metaKey) && event.shiftKey && ["i", "j", "c"].includes(key));
  if (blocked) {
    event.preventDefault();
    toast("考試頁已限制重新整理、檢視原始碼與開發工具快捷鍵。");
  }
}

document.addEventListener("contextmenu", (event) => event.preventDefault());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePopover();
  blockExamShortcuts(event);
});

function currentProblem() { return state.problems[activeIndex] || null; }
function currentProblemKey() { return currentProblem()?.id || ""; }
function currentCodeKey(language = state.language, compilerMode = state.compilerMode) {
  const problemKey = currentProblemKey();
  return problemKey ? `${problemKey}::${compilerMode || "exam"}::${language || ""}` : "";
}
function hasCodeVariantForProblem(problemKey = currentProblemKey()) {
  if (!problemKey) return false;
  const prefix = `${problemKey}::`;
  return Object.keys(state.codeByProblem || {}).some((key) => key.startsWith(prefix));
}
function currentMode() { return monacoModes[state.language] || "plaintext"; }
function currentEditorTheme() { return state.compilerMode === "exam" ? "scintilla-cpe" : "vs"; }
function currentEditorOptions() {
  if (state.compilerMode === "exam") {
    return {
      fontFamily: "'Courier New', 'Lucida Console', 'MingLiU', monospace",
      fontSize: 16,
      fontWeight: "bold",
      fontLigatures: false,
      cursorBlinking: "blink",
      smoothScrolling: false,
      renderLineHighlight: "line",
      scrollbar: {verticalScrollbarSize: 16, horizontalScrollbarSize: 16},
    };
  }
  return {
    fontFamily: '"Cascadia Code", "SFMono-Regular", Consolas, monospace',
    fontSize: 14,
    fontWeight: "normal",
    fontLigatures: true,
    cursorBlinking: "smooth",
    smoothScrolling: true,
    renderLineHighlight: "line",
    scrollbar: {verticalScrollbarSize: 10, horizontalScrollbarSize: 10},
  };
}
function getCode() { return codeEditor ? codeEditor.getValue() : $("code").value; }

function setCode(value) {
  const next = value || "";
  if (codeEditor) {
    if (codeEditor.getValue() !== next) codeEditor.setValue(next);
  } else if ($("code").value !== next) {
    $("code").value = next;
  }
}

function simpleCompilerLanguage(value = state.language) {
  return languageOptions.simple.some(([language]) => language === value) ? value : "exam_cpp_mingw";
}

function switchToSimpleCompiler(message = "已切換成簡易編譯器。", {silent = false} = {}) {
  const currentCode = getCode() || codeForCurrentProblem();

  if (vimMode) setVimMode(false);
  if (codeEditor) {
    codeEditor.dispose();
    codeEditor = null;
  }

  state.compilerMode = "simple";
  state.language = simpleCompilerLanguage(state.languageByCompilerMode?.simple || state.language);
  state.languageByCompilerMode = {
    ...(state.languageByCompilerMode || {}),
    simple: state.language,
  };

  document.body.classList.remove("monaco-ready");
  document.body.classList.add("monaco-failed");

  if ($("compilerMode")) $("compilerMode").value = "simple";
  renderLanguages();
  $("code").value = currentCode || templates[state.language] || "";
  updateToolPermissions();
  saveCurrentBuffers();
  saveState();

  if (!silent && message) toast(message);
}

function leaveSimpleCompiler() {
  document.body.classList.remove("monaco-failed");
  if (!codeEditor) initializeEditor();
}

function normalizeStatus(value) {
  const status = String(value || "").trim().toUpperCase();
  if (status.includes("RUNTIME") || status === "RE") return "RE";
  if (status.includes("TIME") || status === "TLE") return "TLE";
  if (status.includes("COMPIL") || status === "CE") return "CE";
  if (status.includes("WRONG") || status === "WA") return "WA";
  if (status.includes("ACCEPT") || status === "AC" || status === "OK") return "AC";
  return status || "UNKNOWN";
}

function sampleFrom(array, count) {
  const pool = [...array];
  const result = [];
  while (pool.length && result.length < count) {
    result.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
  }
  return result;
}

function drawProblems() {
  if (state.startedAt && !state.endedAt) {
    toast("考試開始後題目固定，不能重新抽題。");
    return;
  }
  const groups = window.CPE_STAR_GROUPS?.problems || {};
  const problems = [];
  for (const [level, count] of drawPlan) {
    for (const problem of sampleFrom(groups[level] || [], count)) problems.push({...problem, level});
  }
  if (problems.length !== 7) {
    toast("CPE 題庫尚未載入完成，請重新整理後再試。");
    return;
  }
  state = {
    ...defaultState(),
    examId: newExamId(),
    mode: $("examMode").value,
    compilerMode: $("compilerMode").value,
    language: $("language").value || "exam_cpp_mingw",
    languageByCompilerMode: {
      ...defaultState().languageByCompilerMode,
      [($("compilerMode").value || "exam")]: $("language").value || "exam_cpp_mingw",
    },
    activeToolTab: state.activeToolTab || "test",
    problems,
  };
  activeIndex = 0;
  applyPermissionsFromMode();
  saveState();
  render();
}

function codeForCurrentProblem() {
  const codeKey = currentCodeKey();
  const problemKey = currentProblemKey();
  if (codeKey && Object.prototype.hasOwnProperty.call(state.codeByProblem, codeKey)) return state.codeByProblem[codeKey] || "";
  if (problemKey && !hasCodeVariantForProblem(problemKey) && Object.prototype.hasOwnProperty.call(state.codeByProblem, problemKey)) return state.codeByProblem[problemKey] || "";
  return templates[state.language] || "";
}

function saveCurrentBuffers(sourceOverride = null) {
  const problemKey = currentProblemKey();
  if (!problemKey) return;
  const codeKey = currentCodeKey();
  if (codeKey) state.codeByProblem[codeKey] = getCode();
  const source = testSourceLabels[sourceOverride] ? sourceOverride : currentTestSource();
  const bufferKey = testBufferKey(problemKey, source);
  state.inputByProblem[bufferKey] = $("input").value;
  state.expectedByProblem[bufferKey] = $("expected").value;
  state.aiByProblem[problemKey] = {question: $("aiQuestion").value, output: $("aiOutput").dataset.raw || $("aiOutput").textContent};
  state.testSource = source;
  saveState();
}

function setActiveProblem(index) {
  if (index < 0 || index >= state.problems.length) return;
  saveCurrentBuffers();
  activeIndex = index;
  render();
}

function renderLanguages() {
  const options = languageOptions[state.compilerMode] || languageOptions.exam;
  $("language").replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }));
  const remembered = state.languageByCompilerMode?.[state.compilerMode];
  if (remembered && options.some(([value]) => value === remembered)) state.language = remembered;
  if (!options.some(([value]) => value === state.language)) state.language = options[0][0];
  state.languageByCompilerMode = {...(state.languageByCompilerMode || {}), [state.compilerMode]: state.language};
  $("language").value = state.language;
}

function applyPermissionsFromMode() {
  const preset = permissionsForMode(state.mode);
  if (preset) state.permissions = preset;
  else state.permissions = {
    aiHelper: $("allowAiHelper").checked,
    aiTests: $("allowAiTests").checked,
    udebug: $("allowUdebug").checked,
    vim: $("allowVim").checked,
  };
}

function syncModeControls() {
  $("examMode").value = state.mode || "standard";
  $("customPermissions").hidden = state.mode !== "custom";

  // 標準、練習、自訂模式都可選擇編譯器；嚴格模式仍固定考試編譯器。
  // 但若 Monaco 失敗而自動切到 simple，仍允許顯示「簡易編譯器」。
  const canChooseCompiler = state.mode !== "strict" || state.compilerMode === "simple";
  $("compilerMode").disabled = !canChooseCompiler;
  if (state.mode === "strict" && state.compilerMode !== "simple") state.compilerMode = "exam";

  $("compilerMode").value = state.compilerMode || "exam";
  $("allowAiHelper").checked = Boolean(state.permissions.aiHelper);
  $("allowAiTests").checked = Boolean(state.permissions.aiTests);
  $("allowUdebug").checked = Boolean(state.permissions.udebug);
  $("allowVim").checked = Boolean(state.permissions.vim);
}

function updateToolPermissions() {
  const p = state.permissions;
  $("vimToggle").disabled = !p.vim || !monacoVimApi || !codeEditor;
  $("vimToggle").title = !p.vim ? "目前模式禁用 Vim" : (!monacoVimApi ? "Vim 模組尚未載入" : "");
  $("openUdebug").disabled = !p.udebug;
  $("aiExpected").disabled = !p.aiTests;
  $("loadTests").disabled = $("testSource").value === "ai" && !p.aiTests;
  $("aiAsk").disabled = !p.aiHelper;
  $("aiHint").disabled = !p.aiHelper;
  $("aiDebug").disabled = !p.aiHelper;
  $("aiTranslate").disabled = !p.aiHelper;
  for (const option of $("testSource").options) {
    if (option.value === "udebug") option.disabled = !p.udebug;
    if (option.value === "ai") option.disabled = !p.aiTests;
  }
  if (($("testSource").value === "udebug" && !p.udebug) || ($("testSource").value === "ai" && !p.aiTests)) {
    switchTestSource("sample", false);
  }
  updateTestSourceUi();
  if (!p.vim && vimMode) setVimMode(false);
}

function applyModeDefaults() {
  saveCurrentBuffers();
  state.mode = $("examMode").value;
  state.languageByCompilerMode = {...(state.languageByCompilerMode || {}), [state.compilerMode]: state.language};
  state.compilerMode = state.mode === "strict" && $("compilerMode").value !== "simple" ? "exam" : $("compilerMode").value;
  applyPermissionsFromMode();
  syncModeControls();
  renderLanguages();
  updateToolPermissions();
  saveState();
}

function startExam() {
  if (isRunning() || isPaused()) return;
  if (!confirm("確定要開始 CPE 考試？開始後題目會固定，計時也會開始。")) return;
  if (!state.problems.length) drawProblems();
  if (!state.problems.length) return;
  state.examId = state.examId || newExamId();
  state.startedAt = state.startedAt || Date.now();
  state.endedAt = null;
  state.pausedAt = null;
  saveState();
  render();
}

function pauseOrResumeExam() {
  if (!state.startedAt || state.endedAt) return;
  if (state.pausedAt) {
    state.pausedTotalMs = Number(state.pausedTotalMs || 0) + (Date.now() - state.pausedAt);
    state.pausedAt = null;
  } else {
    saveCurrentBuffers();
    state.pausedAt = Date.now();
  }
  saveState();
  render();
}

function finishExam(force = false) {
  const forced = force === true;
  if (!state.startedAt || state.endedAt) return;
  if (!forced && !confirm("確定要結束這場 CPE 考試？結束後會保存到歷史紀錄。")) return;
  saveCurrentBuffers();
  state.endedAt = Date.now();
  if (state.pausedAt) {
    state.pausedTotalMs = Number(state.pausedTotalMs || 0) + (state.endedAt - state.pausedAt);
    state.pausedAt = null;
  }
  saveState();
  archiveCurrentExam();
  render();
  if (forced) {
    setTimeout(() => alert("考試時間已到，系統已自動結束並保存到歷史紀錄。"), 0);
  }
}

function submittedRecord(targetState, problemId) {
  const record = targetState.records?.[problemId] || {};
  if (!Object.keys(record).length) return {};
  if (record.source === "uva-submit" || Number(targetState.submitCounts?.[problemId] || 0) > 0) return record;
  return {};
}

function summarizeState(targetState) {
  const records = (targetState.problems || [])
    .map((problem) => submittedRecord(targetState, problem.id))
    .filter((record) => Object.keys(record).length);
  const counts = {};
  for (const record of records) {
    const key = normalizeStatus(record.verdict || record.status);
    counts[key] = (counts[key] || 0) + 1;
  }
  return {
    ac: records.filter((record) => normalizeStatus(record.verdict || record.status) === "AC").length,
    total: targetState.problems?.length || 0,
    counts,
    submitTotal: Object.values(targetState.submitCounts || {}).reduce((sum, value) => sum + Number(value || 0), 0),
    usedMs: elapsedMs(targetState),
  };
}

function archiveCurrentExam() {
  if (!state.startedAt || !state.endedAt) return;
  const entry = {examId: state.examId || `exam-${state.startedAt}`, archivedAt: Date.now(), summary: summarizeState(state), state: JSON.parse(JSON.stringify(state))};
  const history = loadHistory();
  const index = history.findIndex((item) => item.examId === entry.examId);
  if (index >= 0) history[index] = entry;
  else history.unshift(entry);
  saveHistory(history.slice(0, 100));
}

function formatTime(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function updateTimer() {
  if (!state.startedAt) {
    $("timer").textContent = "03:00:00";
    return;
  }
  const left = DURATION_MS - elapsedMs(state);
  $("timer").textContent = formatTime(left);
  if (left <= 0 && !state.endedAt) finishExam(true);
}

function resultClass(status) {
  const normalized = normalizeStatus(status);
  if (normalized === "AC" || normalized === "SUBMITTED") return "ok";
  if (!status || status === "尚未執行" || status === "尚未提交") return "neutral";
  return "bad";
}

function setVerdict(status) {
  $("verdict").textContent = status || "ERROR";
  $("verdict").className = `pill ${resultClass(status)}`;
}

function renderProblemList() {
  $("problemList").replaceChildren(...state.problems.map((problem, index) => {
    const record = submittedRecord(state, problem.id);
    const status = record.verdict || record.status || "未交";
    const button = document.createElement("button");
    button.type = "button";
    button.className = `problem-item${index === activeIndex ? " active" : ""}`;
    button.title = `${problem.id} - ${problem.title || ""}`;
    button.append(
      el("strong", "", `${levelLabels[problem.level] || problem.level} ${problem.id}`),
      el("span", "", problem.title || ""),
      el("span", "", `結果：${status}，提交 ${state.submitCounts[problem.id] || 0} 次`),
    );
    button.addEventListener("click", () => setActiveProblem(index));
    return button;
  }));
}

function renderCurrentProblem() {
  const problem = currentProblem();
  if (!problem) {
    $("problemLevel").textContent = "-";
    $("problemTitle").textContent = "尚未選題";
    setCode("");
    $("input").value = "";
    $("expected").value = "";
    setSidePdf(null);
    setVerdict("尚未執行");
    return;
  }
  $("problemLevel").textContent = levelLabels[problem.level] || problem.level;
  $("problemTitle").textContent = `${problem.id} - ${problem.title || `UVA-${problem.id}`}`;
  $("problemTitle").title = $("problemTitle").textContent;
  if ((state.activeToolTab || "test") === "pdf") setSidePdf(problem);
  setCode(codeForCurrentProblem());
  loadCurrentTestBuffers();
  const ai = state.aiByProblem[problem.id] || {};
  $("aiQuestion").value = ai.question || "";
  setAiOutput(ai.output || "");
  const record = submittedRecord(state, problem.id);
  setVerdict(record.verdict || record.status || "尚未執行");
  renderLastAnswer();
}

function renderLastAnswer() {
  const problem = currentProblem();
  const record = problem ? submittedRecord(state, problem.id) : {};
  if (!problem || !record.time) {
    $("lastAnswer").textContent = "尚無紀錄。";
    return;
  }
  $("lastAnswer").textContent = [
    `題號：${problem.id}`,
    `語言：${record.language || state.language}`,
    `時間：${new Date(record.time).toLocaleString()}`,
    `結果：${record.verdict || record.status || "未知"}`,
    `提交次數：${state.submitCounts[problem.id] || 0}`,
    record.runner ? `Runner：${record.runner}` : "",
    record.submission_id ? `Submission ID：${record.submission_id}` : "",
  ].filter(Boolean).join("\n");
}

function renderSummary() {
  const {ac, counts} = summarizeState(state);
  $("summary").textContent = [
    `AC：${ac} / ${state.problems.length || 7}`,
    `WA：${counts.WA || 0}`,
    `CE：${counts.CE || 0}`,
    `TLE：${counts.TLE || 0}`,
    `RE：${counts.RE || 0}`,
    `總提交：${Object.values(state.submitCounts || {}).reduce((sum, value) => sum + Number(value || 0), 0)}`,
    "",
    ...state.problems.map((problem) => {
      const record = submittedRecord(state, problem.id);
      return `${levelLabels[problem.level] || problem.level} ${problem.id}：${record.verdict || record.status || "未交"}，提交 ${state.submitCounts[problem.id] || 0} 次`;
    }),
  ].join("\n");
}

function historyProblemIds(entry) {
  const problems = entry?.state?.problems || [];
  return problems.map((problem) => String(problem.id || "").trim()).filter(Boolean);
}

function historySubmittedProblemIds(entry) {
  const targetState = entry?.state || {};
  return (targetState.problems || [])
    .filter((problem) => Object.keys(submittedRecord(targetState, problem.id)).length || Number(targetState.submitCounts?.[problem.id] || 0) > 0)
    .map((problem) => String(problem.id || "").trim())
    .filter(Boolean);
}

function renderHistory() {
  const history = loadHistory();
  if (!history.length) {
    $("historyList").textContent = "尚無歷史紀錄。";
    return;
  }
  $("historyList").replaceChildren(...history.map((entry) => {
    const summary = entry.summary || summarizeState(entry.state || {});
    const problemIds = historyProblemIds(entry);
    const submittedIds = historySubmittedProblemIds(entry);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    const startedAt = entry.state?.startedAt ? new Date(entry.state.startedAt).toLocaleString() : "未知時間";
    const problemLine = problemIds.length ? `題號：${problemIds.join("、")}` : "題號：未記錄";
    const submittedLine = submittedIds.length ? `已提交：${submittedIds.join("、")}` : "已提交：尚無";
    button.title = [startedAt, problemLine, submittedLine, entry.examId || ""].filter(Boolean).join("\n");
    button.append(
      el("strong", "", startedAt),
      el("span", "", `AC ${summary.ac || 0} / ${summary.total || 0}，提交 ${summary.submitTotal || 0} 次，用時 ${formatTime(summary.usedMs || 0)}`),
      el("span", "", problemLine),
      el("span", "", submittedLine),
    );
    button.addEventListener("click", () => {
      if (!entry.state) return;
      if (state.startedAt && !state.endedAt && !confirm("目前有進行中的考試，要載入這筆歷史紀錄嗎？")) return;
      state = {...defaultState(), ...JSON.parse(JSON.stringify(entry.state))};
      activeIndex = state.activeIndex || 0;
      saveState();
      render();
      toast("已載入歷史紀錄。");
    });
    return button;
  }));
}

function renderToolTabs() {
  const active = state.activeToolTab || "test";
  $("testTab").classList.toggle("active", active === "test");
  $("pdfTab").classList.toggle("active", active === "pdf");
  $("aiTab").classList.toggle("active", active === "ai");
  $("testTab").setAttribute("aria-selected", String(active === "test"));
  $("pdfTab").setAttribute("aria-selected", String(active === "pdf"));
  $("aiTab").setAttribute("aria-selected", String(active === "ai"));
  $("testPanel").hidden = active !== "test";
  $("pdfPanel").hidden = active !== "pdf";
  $("aiPanel").hidden = active !== "ai";
}

function closePopover() {
  activePopover = null;
  $("popoverLayer").hidden = true;
  for (const config of Object.values(popoverConfig)) $(config.id).hidden = true;
  for (const button of document.querySelectorAll(".menu-button")) button.classList.remove("active");
}

function openPopover(name, anchor) {
  if (activePopover === name) {
    closePopover();
    return;
  }
  closePopover();
  const config = popoverConfig[name];
  if (!config) return;
  activePopover = name;
  const panel = $(config.id);
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(config.width, window.innerWidth - 44);
  const left = Math.max(22, Math.min(rect.left, window.innerWidth - width - 22));
  panel.style.setProperty("--popover-left", `${left}px`);
  panel.style.setProperty("--popover-width", `${width}px`);
  panel.hidden = false;
  $("popoverLayer").hidden = false;
  anchor.classList.add("active");
}

function render() {
  syncModeControls();
  renderLanguages();
  $("examStatus").textContent = isEnded() ? "已結束" : isPaused() ? "暫停中" : isRunning() ? "進行中" : "尚未開始";
  $("drawProblems").disabled = Boolean(state.startedAt && !state.endedAt);
  $("startExam").textContent = isEnded() ? "已結束" : isStarted() ? "考試進行中" : "開始考試";
  $("startExam").disabled = Boolean(isStarted());
  $("pauseExam").textContent = isPaused() ? "繼續" : "暫停";
  $("pauseExam").disabled = !isStarted() || isEnded();
  $("finishExam").disabled = !isStarted() || isEnded();
  $("testSource").value = state.testSource || "sample";
  updateToolPermissions();
  renderToolTabs();
  renderProblemList();
  renderCurrentProblem();
  renderSummary();
  renderHistory();
  updateTimer();
  if (codeEditor && window.monaco) {
    monaco.editor.setModelLanguage(codeEditor.getModel(), currentMode());
    monaco.editor.setTheme(currentEditorTheme());
    codeEditor.updateOptions(currentEditorOptions());
    codeEditor.layout();
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : {detail: await response.text()};
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

async function poll(taskId) {
  for (let i = 0; i < 360; i++) {
    const task = await request(`/api/tasks/${taskId}`);
    if (["SUCCESS", "FAILURE"].includes(task.state)) {
      return task.result || {status: "SE", verdict: "SE", message: "沒有取得執行結果"};
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  throw new Error("等待結果逾時");
}

function recordResult(problem, result, isSubmit) {
  if (!isSubmit) return;
  state.records[problem.id] = {...result, language: state.language, time: Date.now(), source: "uva-submit"};
  state.submitCounts[problem.id] = (state.submitCounts[problem.id] || 0) + 1;
  saveState();
}

function showResult(result) {
  setVerdict(result.verdict || result.status || "ERROR");
  const parts = [];
  if (result.runner) parts.push(`runner:\n${result.runner}`);
  if (result.message) parts.push(result.message);
  if (result.stdout) parts.push(`stdout:\n${result.stdout}`);
  if (result.error_log) parts.push(`error:\n${result.error_log}`);
  $("output").textContent = parts.join("\n\n") || JSON.stringify(result, null, 2);
}

async function execute(kind) {
  const problem = currentProblem();
  if (kind !== "run" && problem && !confirm(`確定要提交目前程式碼到 UVA ${problem.id}？`)) return;
  if (!problem) return toast("請先選題。");
  saveCurrentBuffers();
  const button = kind === "run" ? $("runCode") : $("submitUva");
  button.disabled = true;
  setVerdict(kind === "run" ? "執行中" : "提交中");
  $("output").textContent = "正在送出，請稍候...";
  try {
    const queued = kind === "run"
      ? await request("/api/run", {method: "POST", body: JSON.stringify({code: getCode(), language: state.language, input_data: $("input").value, expected_output: $("expected").value})})
      : await request("/api/uva/submit", {method: "POST", body: JSON.stringify({judge: "uva", problem_id: problem.id, language: state.language, code: getCode()})});
    const result = await poll(queued.task_id);
    recordResult(problem, result, kind !== "run");
    showResult(result);
    renderProblemList();
    renderLastAnswer();
    renderSummary();
  } catch (error) {
    const result = {status: "ERROR", verdict: "ERROR", message: error.message};
    recordResult(problem, result, false);
    showResult(result);
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function parseAiGeneratedTest(message) {
  const inputMatch = message.match(/(?:^|\n)\s*(?:stdin|input)\s*:\s*```(?:text)?\s*\n([\s\S]*?)```/i);
  const expectedMatch = message.match(/(?:^|\n)\s*(?:expected\s+stdout|expected\s+output|stdout|output)\s*:\s*```(?:text)?\s*\n([\s\S]*?)```/i);
  if (!inputMatch || !expectedMatch) return null;
  return {input: inputMatch[1].replace(/\n$/, ""), expected: expectedMatch[1].replace(/\n$/, "")};
}

function parseAiExpectedOutput(message) {
  const match = message.match(/(?:^|\n)\s*(?:expected\s+stdout|expected\s+output|stdout|output)\s*:\s*```(?:text)?\s*\n([\s\S]*?)```/i);
  return match ? match[1].replace(/\n$/, "") : null;
}

async function loadTests() {
  const problem = currentProblem();
  if (!problem) return toast("請先選題。");
  const source = $("testSource").value;
  if (source === "custom") return toast("自訂測資請直接編輯輸入與輸出。");
  if (source === "udebug" && !state.permissions.udebug) return toast("目前模式禁用 uDebug。");
  if (source === "ai" && !state.permissions.aiTests) return toast("目前模式禁用 AI 測資。");
  $("loadTests").disabled = true;
  try {
    if (source === "ai") {
      const queued = await request("/api/ai/help", {method: "POST", body: JSON.stringify({mode: "tests", judge: "uva", problem_id: problem.id, language: state.language, code: getCode(), question: "Generate one useful CPE exam local test case with exact expected stdout."})});
      const result = await poll(queued.task_id);
      const parsed = parseAiGeneratedTest(result.message || "");
      if (!parsed) throw new Error("AI 回答沒有包含可解析的 input/output。");
      $("input").value = parsed.input;
      $("expected").value = parsed.expected;
      setAiOutput(result.message || "");
    } else {
      const queued = await request("/api/tests/fetch", {method: "POST", body: JSON.stringify({judge: "uva", problem_id: problem.id, source})});
      const result = await poll(queued.task_id);
      $("input").value = result.input_data || "";
      $("expected").value = result.expected_output || "";
    }
    saveCurrentBuffers();
    toast("測資已載入。");
  } catch (error) {
    toast(error.message);
  } finally {
    $("loadTests").disabled = false;
  }
}

async function aiExpectedOutput() {
  const problem = currentProblem();
  if (!problem) return toast("請先選題。");
  if (!state.permissions.aiTests) return toast("目前模式禁用 AI 測資。");
  if (!$("input").value.trim()) return toast("請先輸入測試輸入。");
  $("aiExpected").disabled = true;
  try {
    const queued = await request("/api/ai/help", {method: "POST", body: JSON.stringify({mode: "expected", judge: "uva", problem_id: problem.id, language: state.language, code: getCode(), input_data: $("input").value, question: "Compute the exact expected stdout for this stdin."})});
    const result = await poll(queued.task_id);
    const expected = parseAiExpectedOutput(result.message || "") || (result.message || "").trim();
    $("expected").value = expected;
    setAiOutput(result.message || expected);
    saveCurrentBuffers();
  } catch (error) {
    toast(error.message);
  } finally {
    $("aiExpected").disabled = false;
    updateToolPermissions();
  }
}

async function askAi(mode) {
  const problem = currentProblem();
  if (!problem) return toast("請先選題。");
  if (!state.permissions.aiHelper) return toast("目前模式禁用 AI 助手。");
  if (mode === "ask" && !$("aiQuestion").value.trim()) return toast("請先輸入要問 AI 的內容。");
  const button = {ask: $("aiAsk"), hint: $("aiHint"), debug: $("aiDebug"), translate: $("aiTranslate")}[mode];
  button.disabled = true;
  setAiOutput("AI 正在回覆...");
  try {
    const record = state.records[problem.id] || {};
    const queued = await request("/api/ai/help", {method: "POST", body: JSON.stringify({mode, judge: "uva", problem_id: problem.id, language: state.language, code: getCode(), input_data: $("input").value, expected_output: $("expected").value, stdout: record.stdout || "", error_log: record.error_log || "", verdict: record.verdict || record.status || "", question: $("aiQuestion").value})});
    const result = await poll(queued.task_id);
    setAiOutput(result.message || result.error_log || JSON.stringify(result, null, 2));
    saveCurrentBuffers();
  } catch (error) {
    setAiOutput(error.message, true);
    toast(error.message);
  } finally {
    button.disabled = false;
    updateToolPermissions();
  }
}

function openUdebug() {
  const problem = currentProblem();
  if (!problem) return;
  if (!state.permissions.udebug) return toast("目前模式禁用 uDebug。");
  window.open(`https://www.udebug.com/UVa/${problem.id}`, "_blank", "noopener,noreferrer");
}

function setSidePdf(problem) {
  if (!problem) {
    $("sidePdfTitle").textContent = "題目 PDF";
    $("openSidePdf").href = "#";
    $("sideProblemPdf").removeAttribute("src");
    return;
  }
  const title = `UVA ${problem.id} - ${problem.title || ""}`;
  const url = `/api/problems/${problem.id}/pdf`;
  $("sidePdfTitle").textContent = title;
  $("openSidePdf").href = url;
  if ($("sideProblemPdf").getAttribute("src") !== url) $("sideProblemPdf").src = url;
}

function openProblemPdf() {
  const problem = currentProblem();
  if (!problem) return;
  setSidePdf(problem);
  state.activeToolTab = "pdf";
  saveState();
  renderToolTabs();
}

function expandProblemPdf() {
  const problem = currentProblem();
  if (!problem) return;
  $("statementTitle").textContent = `UVA ${problem.id} - ${problem.title || ""}`;
  $("statementFrame").src = `/api/problems/${problem.id}/pdf`;
  $("statementPane").hidden = false;
}

function closeStatement() {
  $("statementPane").hidden = true;
  $("statementFrame").removeAttribute("src");
}

function exportRecord() {
  saveCurrentBuffers();
  if (state.endedAt) archiveCurrentExam();
  const payload = {
    version: 2,
    exportedAt: Date.now(),
    type: "pi-oj-cpe-exam-backup",
    current: JSON.parse(JSON.stringify(state)),
    history: loadHistory(),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `pi-oj-cpe-exam-backup-${new Date().toISOString().slice(0, 19).replaceAll(":", "-")}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

function normalizeImportedHistory(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.history)) return data.history;
  if (data?.state) return [{examId: data.examId || data.state.examId || `import-${Date.now()}`, archivedAt: data.archivedAt || Date.now(), summary: data.summary || summarizeState(data.state), state: data.state}];
  if (data?.problems && data?.records) return [{examId: data.examId || `import-${Date.now()}`, archivedAt: Date.now(), summary: summarizeState(data), state: data}];
  return [];
}

function normalizeImportedCurrent(data) {
  if (data?.current?.problems) return {...defaultState(), ...data.current};
  if (data?.type === "pi-oj-cpe-exam-backup" && data?.current) return {...defaultState(), ...data.current};
  if (data?.problems && !data?.archivedAt) return {...defaultState(), ...data};
  return null;
}

async function importRecordFile(file) {
  if (!file) return;
  try {
    {
    const data = JSON.parse(await file.text());
    const importedHistory = normalizeImportedHistory(data);
    const importedCurrent = normalizeImportedCurrent(data);
    if (!importedHistory.length && !importedCurrent) throw new Error("這不是可匯入的 CPE 考試備份或歷史紀錄。");
    const byId = new Map(loadHistory().map((entry) => [entry.examId, entry]));
    for (const entry of importedHistory) {
      const normalized = {examId: entry.examId || entry.state?.examId || newExamId(), archivedAt: entry.archivedAt || Date.now(), summary: entry.summary || summarizeState(entry.state || {}), state: entry.state || entry};
      byId.set(normalized.examId, normalized);
    }
    saveHistory([...byId.values()].sort((a, b) => (b.archivedAt || 0) - (a.archivedAt || 0)).slice(0, 100));
    if (importedCurrent) {
      const hasActiveExam = Boolean(state.startedAt && !state.endedAt);
      const shouldRestore = confirm(hasActiveExam
        ? "備份裡包含目前考試狀態。要覆蓋現在進行中的考試並恢復備份嗎？"
        : "備份裡包含目前考試狀態。要恢復成目前考試嗎？");
      if (shouldRestore) {
        state = {
          ...defaultState(),
          ...importedCurrent,
          permissions: {...defaultState().permissions, ...(importedCurrent.permissions || {})},
          pausedTotalMs: Number(importedCurrent.pausedTotalMs || 0),
        };
        activeIndex = Math.min(Math.max(Number(state.activeIndex || 0), 0), Math.max((state.problems || []).length - 1, 0));
        saveState();
        render();
        toast("已恢復備份中的目前考試。");
        return;
      }
    }
    renderHistory();
    toast(`已匯入 ${importedHistory.length} 筆歷史紀錄。`);
    return;
    }
    const imported = normalizeImportedHistory(JSON.parse(await file.text()));
    if (!imported.length) throw new Error("這不是可匯入的 CPE 考試紀錄。");
    const byId = new Map(loadHistory().map((entry) => [entry.examId, entry]));
    for (const entry of imported) {
      const normalized = {examId: entry.examId || entry.state?.examId || newExamId(), archivedAt: entry.archivedAt || Date.now(), summary: entry.summary || summarizeState(entry.state || {}), state: entry.state || entry};
      byId.set(normalized.examId, normalized);
    }
    saveHistory([...byId.values()].sort((a, b) => (b.archivedAt || 0) - (a.archivedAt || 0)).slice(0, 100));
    renderHistory();
    toast(`已匯入 ${imported.length} 筆紀錄。`);
  } catch (error) {
    toast(error.message);
  } finally {
    $("importFile").value = "";
  }
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`無法載入 ${src}`));
    document.head.appendChild(script);
  });
}

async function resourceExists(url) {
  try { return (await fetch(url, {method: "HEAD", cache: "no-store"})).ok; }
  catch { return false; }
}

async function ensureMonacoLoader() {
  if (typeof window.require === "function" && typeof window.require.config === "function") return;
  try {
    await loadScript(`/static/vendor/monaco/vs/loader.js?v=${MONACO_VERSION}`);
  } catch {
    monacoLoaderSource = "cdn";
    await loadScript(`https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs/loader.js`);
  }
}

function defineExamTheme() {
  monaco.editor.defineTheme("scintilla-cpe", {
    base: "vs",
    inherit: true,
    rules: [
      {token: "keyword", foreground: "0000FF", fontStyle: "bold"},
      {token: "type", foreground: "0000FF", fontStyle: "bold"},
      {token: "comment", foreground: "008000", fontStyle: "italic"},
      {token: "string", foreground: "FF00FF"},
      {token: "number", foreground: "FF0000"},
      {token: "predefined", foreground: "0000FF"},
    ],
    colors: {
      "editor.background": "#FFFFFF",
      "editor.foreground": "#000000",
      "editorGutter.background": "#FFFFFF",
      "editorLineNumber.foreground": "#808080",
      "editorLineNumber.activeForeground": "#000000",
      "editorCursor.foreground": "#000000",
      "editor.selectionBackground": "#b5d5ff",
      "editor.inactiveSelectionBackground": "#d9e9ff",
      "editor.lineHighlightBackground": "#F0F0F0",
      "editor.lineHighlightBorder": "#F0F0F0",
      "editorIndentGuide.background1": "#e6e6e6",
      "editorIndentGuide.activeBackground1": "#bdbdbd",
    },
  });
}

async function initializeEditor() {
  if (state.compilerMode === "simple") {
    switchToSimpleCompiler("", {silent: true});
    return;
  }

  try {
    await ensureMonacoLoader();
    const useCdn = monacoLoaderSource === "cdn";
    const vsPath = useCdn ? `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs` : "/static/vendor/monaco/vs";
    const localVim = !useCdn && await resourceExists("/static/vendor/monaco-vim.js");
    window.require.config({
      paths: {
        vs: vsPath,
        "monaco-vim": localVim ? "/static/vendor/monaco-vim" : `https://cdn.jsdelivr.net/npm/monaco-vim@${MONACO_VIM_VERSION}/dist/monaco-vim.umd`,
      },
    });
    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) switchToSimpleCompiler("Monaco 載入超過 12 秒，已切換成簡易編譯器。你仍然可以正常寫程式與執行測試。");
    }, 12000);
    window.require(["vs/editor/editor.main"], () => {
      settled = true;
      clearTimeout(timeout);
      if (document.body.classList.contains("monaco-failed") || state.compilerMode === "simple") return;
      defineExamTheme();
      codeEditor = monaco.editor.create($("editor"), {
        value: codeForCurrentProblem(),
        language: currentMode(),
        theme: currentEditorTheme(),
        automaticLayout: true,
        minimap: {enabled: false},
        fontFamily: "'Courier New', Consolas, monospace",
        fontSize: 15,
        lineHeight: 22,
        scrollBeyondLastLine: false,
        tabSize: 4,
        insertSpaces: true,
        tabFocusMode: false,
        renderWhitespace: "none",
        cursorBlinking: "blink",
        cursorStyle: "line",
        lineNumbersMinChars: 4,
        glyphMargin: false,
        folding: false,
        overviewRulerLanes: 0,
        hideCursorInOverviewRuler: true,
        renderLineHighlight: "line",
        scrollbar: {verticalScrollbarSize: 12, horizontalScrollbarSize: 12},
        ...currentEditorOptions(),
      });
      codeEditor.updateOptions(currentEditorOptions());
      codeEditor.onDidChangeModelContent(saveCurrentBuffers);
      document.body.classList.add("monaco-ready");
      if (typeof window.define === "function") {
        window.define(
          "monaco-editor/esm/vs/editor/editor.api",
          ["vs/editor/editor.main"],
          (editorApi) => editorApi,
        );
      }
      window.require(["monaco-vim"], (MonacoVim) => {
        monacoVimApi = MonacoVim?.initVimMode
          ? MonacoVim
          : (MonacoVim?.default?.initVimMode ? MonacoVim.default : globalThis.MonacoVim);
        updateToolPermissions();
      }, (error) => {
        console.error("monaco-vim initialization failed", error);
        monacoVimApi = null;
        updateToolPermissions();
      });
    }, (error) => {
      settled = true;
      clearTimeout(timeout);
      console.error(error);
      switchToSimpleCompiler("Monaco 初始化失敗，已切換成簡易編譯器。");
    });
  } catch (error) {
    console.error(error);
    switchToSimpleCompiler("Monaco 載入失敗，已切換成簡易編譯器。");
  }
}

function setVimMode(enabled) {
  if (enabled && (!state.permissions.vim || !codeEditor || !monacoVimApi?.initVimMode)) return toast("目前不能使用 Vim。");
  if (enabled && !vimMode) {
    $("vimStatus").hidden = false;
    vimMode = monacoVimApi.initVimMode(codeEditor, $("vimStatus"));
    $("vimToggle").textContent = "Vim ON";
    $("vimToggle").setAttribute("aria-pressed", "true");
    codeEditor.focus();
  } else if (!enabled && vimMode) {
    vimMode.dispose();
    vimMode = null;
    $("vimStatus").hidden = true;
    $("vimToggle").textContent = "Vim OFF";
    $("vimToggle").setAttribute("aria-pressed", "false");
  }
}

ensureTestUi();
ensurePdfUi();

document.querySelectorAll(".menu-button").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    openPopover(button.dataset.popover, button);
  });
});
$("popoverLayer").addEventListener("click", (event) => {
  if (event.target === $("popoverLayer")) closePopover();
});
document.addEventListener("click", (event) => {
  if (!activePopover) return;
  if (event.target.closest(".popover-panel") || event.target.closest(".menu-button")) return;
  closePopover();
});
$("testTab").addEventListener("click", () => {
  state.activeToolTab = "test";
  saveState();
  renderToolTabs();
  codeEditor?.layout();
});
$("pdfTab").addEventListener("click", () => {
  state.activeToolTab = "pdf";
  saveState();
  renderToolTabs();
  codeEditor?.layout();
});
$("aiTab").addEventListener("click", () => {
  state.activeToolTab = "ai";
  saveState();
  renderToolTabs();
  codeEditor?.layout();
});
$("examMode").addEventListener("change", applyModeDefaults);
$("compilerMode").addEventListener("change", () => {
  saveCurrentBuffers();
  state.languageByCompilerMode = {...(state.languageByCompilerMode || {}), [state.compilerMode]: state.language};
  state.compilerMode = $("compilerMode").value;
  renderLanguages();
  state.language = $("language").value;

  if (state.compilerMode === "simple") {
    switchToSimpleCompiler("已切換為簡易編譯器。");
  } else {
    leaveSimpleCompiler();
    render();
    saveState();
  }
});
$("language").addEventListener("change", () => {
  saveCurrentBuffers();
  state.language = $("language").value;
  state.languageByCompilerMode = {...(state.languageByCompilerMode || {}), [state.compilerMode]: state.language};
  if (codeEditor && window.monaco) monaco.editor.setModelLanguage(codeEditor.getModel(), currentMode());
  setCode(codeForCurrentProblem());
  saveCurrentBuffers();
});
["allowAiHelper", "allowAiTests", "allowUdebug", "allowVim"].forEach((id) => $(id).addEventListener("change", applyModeDefaults));
$("drawProblems").addEventListener("click", drawProblems);
$("startExam").addEventListener("click", startExam);
$("pauseExam").addEventListener("click", pauseOrResumeExam);
$("finishExam").addEventListener("click", () => finishExam(false));
if ($("openProblem")) $("openProblem").addEventListener("click", openProblemPdf);
$("expandPdf").addEventListener("click", expandProblemPdf);
$("closeStatement").addEventListener("click", closeStatement);
$("runCode").addEventListener("click", () => execute("run"));
$("submitUva").addEventListener("click", () => execute("submit"));
$("loadTests").addEventListener("click", loadTests);
$("openUdebug").addEventListener("click", openUdebug);
$("aiExpected").addEventListener("click", aiExpectedOutput);
$("aiAsk").addEventListener("click", () => askAi("ask"));
$("aiHint").addEventListener("click", () => askAi("hint"));
$("aiDebug").addEventListener("click", () => askAi("debug"));
$("aiTranslate").addEventListener("click", () => askAi("translate"));
$("vimToggle").addEventListener("click", () => setVimMode(!vimMode));
$("exportRecord").addEventListener("click", exportRecord);
$("importRecord").addEventListener("click", () => $("importFile").click());
$("importFile").addEventListener("change", (event) => importRecordFile(event.target.files?.[0]));
$("code").addEventListener("input", saveCurrentBuffers);
$("code").addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  event.preventDefault();
  const target = event.target;
  const start = target.selectionStart;
  const end = target.selectionEnd;
  target.setRangeText("    ", start, end, "end");
  saveCurrentBuffers();
});
$("input").addEventListener("input", saveCurrentBuffers);
$("expected").addEventListener("input", saveCurrentBuffers);
$("aiQuestion").addEventListener("input", saveCurrentBuffers);
$("testSource").addEventListener("change", () => {
  switchTestSource($("testSource").value);
  updateToolPermissions();
});

activeIndex = Math.min(Math.max(Number(state.activeIndex || 0), 0), Math.max(state.problems.length - 1, 0));
renderLanguages();
if (!state.problems.length) drawProblems();
render();
initializeEditor();
setInterval(updateTimer, 1000);
