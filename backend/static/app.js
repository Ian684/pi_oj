const $ = (id) => document.getElementById(id);
const root = document.documentElement;
let codeEditor = null;
let monacoVimApi = null;
let vimMode = null;
let currentPdfObjectUrl = null;
let lastSubmittedExpectedOutput = "";
let lastRunResult = null;
let monacoLoaderSource = "local";

const RUN_RESULT_STORAGE_KEY = "pi-oj-last-run-result";

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
  uva_c: `#include <stdio.h>

int main(void) {
    // Write code here
    return 0;
}
`,
  uva_java: `import java.io.*;
import java.util.*;

public class Main {
    public static void main(String[] args) throws Exception {
        // Write code here
    }
}
`,
  uva_pascal: `program Main;
begin
  { Write code here }
end.
`,
  uva_cpp11: `#include <iostream>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Write code here
    return 0;
}
`,
  uva_py3: `# Write code here

def main():
    pass

if __name__ == "__main__":
    main()
`,
  cf_c11: `#include <stdio.h>

int main(void) {
    // Write code here
    return 0;
}
`,
  cf_cpp17: `#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Write code here
    return 0;
}
`,
  cf_cpp20: `#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Write code here
    return 0;
}
`,
  cf_cpp23: `#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Write code here
    return 0;
}
`,
  cf_java21: `import java.io.*;
import java.util.*;

public class Main {
    public static void main(String[] args) throws Exception {
        // Write code here
    }
}
`,
  cf_py313: `# Write code here

def main():
    pass

if __name__ == "__main__":
    main()
`,
  cf_pypy310: `# Write code here

def main():
    pass

if __name__ == "__main__":
    main()
`,
  cf_js_v8: `// Codeforces JavaScript V8 uses readline().
// Write code here
`,
  cf_node: `const fs = require("fs");
const input = fs.readFileSync(0, "utf8").trimEnd();

// Write code here
`,
  cf_rust2021: `use std::io::{self, Read};

fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    // Write code here
}
`,
  cf_rust2024: `use std::io::{self, Read};

fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    // Write code here
}
`,
  cf_go: `package main

import "fmt"

func main() {
    // Write code here
    _ = fmt.Println
}
`,
  cf_perl: `use strict;
use warnings;

# Write code here
`,
  cf_php: `<?php
$input = stream_get_contents(STDIN);
// Write code here
`,
  cf_ruby: `input = STDIN.read
# Write code here
`,
  cf_ocaml: `(* Write code here *)
`,
  cf_d: `import std.stdio;

void main() {
    // Write code here
}
`,
};

const languageOptionsByJudge = {
  uva: [["uva_c", "ANSI C 5.3.0"], ["uva_java", "JAVA 1.8.0"], ["uva_pascal", "PASCAL 3.0.0"], ["uva_cpp11", "C++11 5.3.0"], ["uva_py3", "PYTH3 3.5.1"]],
  codeforces: [["cf_c11", "GNU GCC C11 5.1.0"], ["cf_cpp17", "GNU G++17 7.3.0"], ["cf_cpp20", "GNU G++20 13.2"], ["cf_cpp23", "GNU G++23 14.2"], ["cf_java21", "Java 21 64bit"], ["cf_py313", "Python 3.13.2"], ["cf_pypy310", "PyPy 3.10"], ["cf_js_v8", "JavaScript V8"], ["cf_node", "Node.js 15.8.0"], ["cf_rust2021", "Rust 1.89.0 (2021)"], ["cf_rust2024", "Rust 1.89.0 (2024)"], ["cf_go", "Go 1.22.2"], ["cf_perl", "Perl 5.20.1"], ["cf_php", "PHP 8.1.7"], ["cf_ruby", "Ruby 3.2.2"], ["cf_ocaml", "OCaml 4.02.1"], ["cf_d", "D DMD32 2.105.0"]],
};
const monacoLanguageModes = {uva_c:"c", uva_java:"java", uva_pascal:"pascal", uva_cpp11:"cpp", uva_py3:"python", cf_c11:"c", cf_cpp17:"cpp", cf_cpp20:"cpp", cf_cpp23:"cpp", cf_java21:"java", cf_py313:"python", cf_pypy310:"python", cf_js_v8:"javascript", cf_node:"javascript", cf_rust2021:"rust", cf_rust2024:"rust", cf_go:"go", cf_perl:"perl", cf_php:"php", cf_ruby:"ruby", cf_ocaml:"ocaml", cf_d:"d"};
const languageAliases = {cpp:"uva_cpp11", cpp17:"cf_cpp17", cpp20:"cf_cpp20", c:"cf_c11", py:"cf_py313", java:"cf_java21", js:"cf_node"};
function defaultLanguageForJudge(judge = getJudge()) { return judge === "codeforces" ? "cf_cpp17" : "uva_cpp11"; }
function languageStorageKey(judge = getJudge()) { return `pi-oj-language-${judge}`; }
function normalizeLanguage(value, judge = getJudge()) {
  const options = languageOptionsByJudge[judge] || languageOptionsByJudge.uva;
  const normalized = languageAliases[value] || value || defaultLanguageForJudge(judge);
  return options.some(([id]) => id === normalized) ? normalized : defaultLanguageForJudge(judge);
}
function renderLanguageOptions(judge = getJudge()) {
  const select = $("language");
  const options = languageOptionsByJudge[judge] || languageOptionsByJudge.uva;
  const nextValue = normalizeLanguage(localStorage.getItem(languageStorageKey(judge)) || select.value, judge);
  select.replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }));
  select.value = nextValue;
  activeLanguage = nextValue;
  localStorage.setItem(languageStorageKey(judge), nextValue);
}
function currentLanguageMode() { return monacoLanguageModes[$("language").value] || "plaintext"; }

const saved = JSON.parse(localStorage.getItem("pi-oj-code") || "{}");
const savedAiState = JSON.parse(localStorage.getItem("pi-oj-ai") || "{}");
let activeLanguage = defaultLanguageForJudge(effectiveJudge(localStorage.getItem("pi-oj-judge") || "uva"));
$("judge").value = localStorage.getItem("pi-oj-judge") || "uva";
renderLanguageOptions(effectiveJudge());
$("code").value = saved[activeLanguage] || templates[activeLanguage] || "";
$("aiQuestion").value = savedAiState.question || "";
$("problemId").value = getJudge() === "cpe"
  ? localStorage.getItem("pi-oj-problem-id-cpe") || ""
  : localStorage.getItem(`pi-oj-problem-id-${getJudge()}`) || "";
updateJudgeUi();
const savedTests = JSON.parse(localStorage.getItem("pi-oj-tests") || "{}");
let activeTestSource = "sample";

function renderCpeStarOptions() {
  const select = $("cpeStar");
  const levelSelect = $("cpeLevel");
  if (!select) return;
  const groups = window.CPE_STAR_GROUPS || {labels: {}, problems: {}};
  const level = levelSelect?.value || "must";
  const problems = groups.problems?.[level] || [];
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = problems.length ? "Select problem..." : "No list loaded";
  select.appendChild(empty);
  for (const problem of problems) {
    const option = document.createElement("option");
    option.value = String(problem.id || "");
    option.textContent = [problem.id, problem.title].filter(Boolean).join(" - ");
    select.appendChild(option);
  }
  const savedCpeProblem = localStorage.getItem("pi-oj-problem-id-cpe") || "";
  if (savedCpeProblem && problems.some((problem) => String(problem.id) === savedCpeProblem)) {
    select.value = savedCpeProblem;
  }
  select.disabled = problems.length === 0;
  if (levelSelect) {
    for (const option of levelSelect.options) {
      const count = groups.problems?.[option.value]?.length || 0;
      const label = groups.labels?.[option.value] || option.textContent.replace(/\s*\(\d+\)$/, "");
      option.textContent = count ? `${label} (${count})` : label;
    }
  }
}

function applyCpeStarProblem(problemId) {
  if (!problemId) return;
  $("problemId").value = problemId;
  localStorage.setItem("pi-oj-problem-id-cpe", problemId);
  localStorage.setItem("pi-oj-problem-id-uva", problemId);
}



function getJudge() {
  return $("judge")?.value || "uva";
}

function effectiveJudge(judge = getJudge()) {
  return judge === "cpe" ? "uva" : judge;
}

function codeforcesProblemCode(problemId) {
  const match = problemId.trim().toUpperCase().replace("-", "/").match(/^(\d{1,6})\s*\/?\s*([A-Z][0-9A-Z]?)$/);
  return match ? `${match[1]}${match[2]}` : "";
}

function problemUrlForJudge(judge, problemId) {
  if (effectiveJudge(judge) === "codeforces") {
    const match = problemId.trim().toUpperCase().replace("-", "/").match(/^(\d{1,6})\s*\/?\s*([A-Z][0-9A-Z]?)$/);
    if (!match) return "";
    return `https://codeforces.com/problemset/problem/${match[1]}/${match[2]}`;
  }
  return `/api/problems/${problemId}/pdf`;
}

function codeforcesSubmitUrl(problemId) {
  const problemCode = codeforcesProblemCode(problemId);
  return `https://codeforces.com/problemset/submit${problemCode ? `?submittedProblemCode=${encodeURIComponent(problemCode)}` : ""}`;
}

function validProblemId(value) {
  const judge = effectiveJudge();
  if (judge === "codeforces") return /^(\d{1,6})\s*\/?\s*([A-Za-z][0-9A-Za-z]?)$/.test(value.trim());
  return validUvaProblemId(value);
}

function syncTestSourceOptions() {
  const allowUdebug = effectiveJudge() === "uva";
  const udebugOption = $("testSource").querySelector('option[value="udebug"]');
  if (udebugOption) udebugOption.hidden = !allowUdebug;
  if (!allowUdebug && $("testSource").value === "udebug") $("testSource").value = "sample";
}

function updateJudgeUi() {
  const judge = getJudge();
  const label = $("problemLabel");
  const isCpe = judge === "cpe";
  const isCodeforces = judge === "codeforces";
  if ($("cpeLabel")) $("cpeLabel").hidden = !isCpe;
  if (label) label.hidden = isCpe;
  if (isCodeforces) {
    if (label) label.childNodes[0].textContent = "Codeforces " + "\u984c\u865f" + " ";
    $("problemId").placeholder = "1843A";
    $("pdfToggle").textContent = "\u984c\u76ee\u9801";
    $("uva").textContent = "\u2191 " + "\u63d0\u4ea4" + " Codeforces";
  } else {
    if (label) label.childNodes[0].textContent = "UVA " + "\u984c\u865f" + " ";
    $("problemId").placeholder = "100-1800 / 10000-13400";
    $("pdfToggle").textContent = "\u984c\u76ee PDF";
    $("uva").textContent = "\u2191 " + "\u63d0\u4ea4" + " UVA";
  }
  syncTestSourceOptions();
}





const testSourceLabels = {
  sample: ["\u984c\u76ee\u8f38\u5165", "\u984c\u76ee\u8f38\u51fa\uff08\u7a7a\u767d\u6642\u53ea\u57f7\u884c\uff09"],
  udebug: ["uDebug \u8f38\u5165", "uDebug \u8f38\u51fa"],
  custom: ["\u81ea\u8a02\u6e2c\u8cc7\u8f38\u5165", "\u81ea\u8a02\u6e2c\u8cc7\u8f38\u51fa\uff08\u7a7a\u767d\u6642\u53ea\u57f7\u884c\uff09"],
  ai: ["AI \u6e2c\u8cc7\u8f38\u5165", "AI \u6e2c\u8cc7\u9810\u671f\u8f38\u51fa"],
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
  $("fetchTests").hidden = !["sample", "ai", "custom"].includes(source);
  $("fetchTests").textContent = source === "ai" ? "AI 產生" : source === "custom" ? "AI 算輸出" : "載入";
  $("openUdebug").hidden = !(source === "udebug" && effectiveJudge() === "uva");
  localStorage.setItem("pi-oj-test-source", source);
}


function invalidProblemMessage() {
  return effectiveJudge() === "codeforces"
    ? "Codeforces 題號格式需像 1843A 或 1843/A"
    : "UVA 題號範圍需為 100-1800 或 10000-13400";
}


async function fetchSelectedTests() {
  const problemId = $("problemId").value;
  const source = activeTestSource;
  if (!validProblemId(problemId)) {
    toast(invalidProblemMessage());
    return;
  }
  if (!["sample", "udebug", "ai", "custom"].includes(source)) return;

  const button = $("fetchTests");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = source === "ai" ? "AI \u7522\u751f\u4e2d\u2026" : source === "custom" ? "AI \u8a08\u7b97\u4e2d\u2026" : "\u8f09\u5165\u4e2d\u2026";
  try {
    let result;
    if (["sample", "udebug"].includes(source)) {
      const queued = await request("/api/tests/fetch", {
        method: "POST",
        body: JSON.stringify({judge: effectiveJudge(), problem_id: problemId, source}),
      });
      result = await poll(queued.task_id);
      if (!result || result.status !== "FETCHED") {
        throw new Error(result?.error_log || "\u984c\u76ee\u6e2c\u8cc7\u8f09\u5165\u5931\u6557");
      }
    } else if (source === "ai") {
      switchSidePanel("ai");
      const pageName = effectiveJudge() === "codeforces" ? "Codeforces 題目頁" : "UVA PDF";
      setAiOutput(`AI \u52a9\u6559\u6b63\u5728\u95b1\u8b80 ${pageName}\uff0c\u4e26\u7522\u751f input/output \u6e2c\u8cc7\u2026`);
      const aiResult = await requestAi("tests");
      showAiResult(aiResult);
      const parsed = parseAiGeneratedTest(aiResult.message || "");
      if (!parsed) {
        throw new Error("AI \u56de\u61c9\u4e2d\u627e\u4e0d\u5230\u5b8c\u6574\u7684 stdin / expected stdout \u5340\u584a");
      }
      switchSidePanel("test");
      result = {
        status: "FETCHED",
        source,
        input_data: parsed.input,
        expected_output: parsed.expected,
        message: "\u5df2\u5957\u7528 AI \u7522\u751f\u7684\u6e2c\u8cc7",
      };
    } else {
      if (!$("input").value.trim()) {
        throw new Error("\u8acb\u5148\u5728\u81ea\u8a02\u6e2c\u8cc7\u8f38\u5165 stdin\uff0c\u518d\u8acb AI \u8a08\u7b97\u8f38\u51fa");
      }
      switchSidePanel("ai");
      setAiOutput("AI \u52a9\u6559\u6b63\u5728\u6839\u64da\u4f60\u7684\u81ea\u8a02 stdin \u8a08\u7b97 expected stdout\u2026");
      const aiResult = await requestAi("expected");
      showAiResult(aiResult);
      const expected = parseAiExpectedOutput(aiResult.message || "");
      if (expected === null) {
        throw new Error("AI \u56de\u61c9\u4e2d\u627e\u4e0d\u5230 expected stdout \u5340\u584a");
      }
      switchSidePanel("test");
      result = {
        status: "FETCHED",
        source,
        input_data: $("input").value,
        expected_output: expected,
        message: "\u5df2\u4fdd\u7559\u4f60\u7684\u8f38\u5165\u4e26\u5957\u7528 AI \u8a08\u7b97\u7684\u8f38\u51fa",
      };
    }

    savedTests[source] = {
      input: result.input_data,
      expected: result.expected_output,
    };
    localStorage.setItem("pi-oj-tests", JSON.stringify(savedTests));
    if (activeTestSource === source) {
      if (source !== "custom") $("input").value = result.input_data;
      $("expected").value = result.expected_output;
    }
    toast(result.message || "\u6e2c\u8cc7\u5df2\u5957\u7528");
  } catch (error) {
    toast(error.message);
    if (["ai", "custom"].includes(source)) setAiOutput(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function openProblemPdf() {
  const problemId = $("problemId").value;
  if (!validProblemId(problemId)) {
    toast(invalidProblemMessage());
    return;
  }

  const button = $("pdfToggle");
  const judge = effectiveJudge();
  const statementId = problemId.trim().toUpperCase().replace(/[^0-9A-Z]/g, "");
  const pdfUrl = judge === "codeforces" ? `/api/problems/codeforces/${statementId}/statement` : problemUrlForJudge(judge, problemId);
  button.disabled = true;
  $("pdfTitle").textContent = `${judge === "codeforces" ? "Codeforces" : "UVA"} ${problemId} \u984c\u76ee`;
  $("openPdf").href = problemUrlForJudge(judge, problemId) || pdfUrl;
  $("pdfPane").hidden = false;
  document.querySelector("main").classList.add("pdf-open");
  try {
    if (judge === "codeforces") {
      if (currentPdfObjectUrl) URL.revokeObjectURL(currentPdfObjectUrl);
      currentPdfObjectUrl = null;
      $("problemPdf").src = pdfUrl;
      $("testSource").value = "sample";
      switchTestSource("sample");
      await fetchSelectedTests();
      return;
    }
    const response = await fetch(pdfUrl);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `PDF \u8f09\u5165\u5931\u6557\uff08HTTP ${response.status}\uff09`);
    }
    const blob = await response.blob();
    if (currentPdfObjectUrl) URL.revokeObjectURL(currentPdfObjectUrl);
    currentPdfObjectUrl = URL.createObjectURL(blob);
    $("problemPdf").src = currentPdfObjectUrl;
    $("testSource").value = "sample";
    switchTestSource("sample");
    await fetchSelectedTests();
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
      language: currentLanguageMode(),
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

function saveRunResultState(state) {
  try {
    localStorage.setItem(RUN_RESULT_STORAGE_KEY, JSON.stringify({
      ...state,
      savedAt: Date.now(),
    }));
  } catch (error) {
    console.warn("儲存執行結果失敗", error);
  }
}

function restoreRunResultState() {
  try {
    const state = JSON.parse(localStorage.getItem(RUN_RESULT_STORAGE_KEY) || "null");
    if (!state || !state.result) return;

    lastSubmittedExpectedOutput = state.expectedOutput || "";

    showResult(state.result, {
      persist: false,
      testSource: state.testSource || activeTestSource,
      expectedOutput: state.expectedOutput || "",
    });
  } catch (error) {
    console.warn("還原執行結果失敗", error);
    localStorage.removeItem(RUN_RESULT_STORAGE_KEY);
  }
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

function showResult(result, options = {}) {
  const persist = options.persist !== false;
  const resultTestSource = options.testSource || activeTestSource;
  const expectedForDiff = options.expectedOutput !== undefined
    ? options.expectedOutput
    : (lastSubmittedExpectedOutput || $("expected").value);

  lastRunResult = result;

  if (persist) {
    saveRunResultState({
      result,
      testSource: resultTestSource,
      expectedOutput: expectedForDiff,
    });
  }

  $("output").classList.remove("ai-output");
  setVerdict(result.status || "SE");

  const isUva = Boolean(result.uva_submission_id);
  $("resultMeta").hidden = false;

  const localSourceNames = {
    sample: "題目範例",
    udebug: "uDebug",
    ai: "AI 測資",
    custom: "自訂測資",
  };

  $("resultSource").textContent = isUva
    ? `UVA ${result.problem_id || ""}`.trim()
    : `本機 — ${localSourceNames[resultTestSource] || resultTestSource}`;

  $("resultId").textContent = result.uva_submission_id || result.submission_id || "—";
  $("resultVerdict").textContent = result.verdict || result.status || "—";
  $("resultTime").textContent = result.cpu_time || "—";

  $("output").classList.remove("diff-output");

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

function sanitizeAiHtml(html) {
  const template = document.createElement("template");
  template.innerHTML = html;
  const allowedTags = new Set([
    "A", "BLOCKQUOTE", "BR", "CODE", "DEL", "EM", "H1", "H2", "H3", "H4",
    "H5", "H6", "HR", "LI", "OL", "P", "PRE", "STRONG", "TABLE", "TBODY",
    "TD", "TH", "THEAD", "TR", "UL",
  ]);
  const allowedAttrs = new Set(["class", "href", "rel", "target"]);
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_ELEMENT);
  const remove = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!allowedTags.has(node.tagName)) {
      remove.push(node);
      continue;
    }
    for (const attr of [...node.attributes]) {
      if (!allowedAttrs.has(attr.name)) {
        node.removeAttribute(attr.name);
        continue;
      }
      if (attr.name === "href" && !/^(https?:|mailto:|#)/i.test(attr.value)) {
        node.removeAttribute(attr.name);
      }
    }
    if (node.tagName === "A") {
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    }
  }
  for (const node of remove) {
    node.replaceWith(document.createTextNode(node.textContent || ""));
  }
  return template.innerHTML;
}

function renderAiMath(target) {
  if (typeof renderMathInElement !== "function") return;
  renderMathInElement(target, {
    delimiters: [
      {left: "$$", right: "$$", display: true},
      {left: "\\[", right: "\\]", display: true},
      {left: "$", right: "$", display: false},
      {left: "\\(", right: "\\)", display: false},
    ],
    ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    throwOnError: false,
  });
}

function renderAiReadable(message, isError = false) {
  const target = $("aiOutput");
  target.textContent = "";
  target.classList.toggle("ai-error", isError);
  target.classList.toggle("ai-readable", !isError);

  if (!message) {
    target.textContent = "AI " + "\u56de\u7b54\u6703\u986f\u793a\u5728\u9019\u88e1";
    return;
  }

  if (isError || !window.marked) {
    const pre = document.createElement("pre");
    pre.textContent = message;
    target.appendChild(pre);
    return;
  }

  const html = marked.parse(String(message).replaceAll("\r\n", "\n").replaceAll("\r", "\n"), {
    async: false,
    breaks: true,
    gfm: true,
  });
  target.innerHTML = sanitizeAiHtml(html);
  renderAiMath(target);
}

function saveAiState(next = {}) {
  const state = {
    question: $("aiQuestion").value,
    output: savedAiState.output || "",
    isError: Boolean(savedAiState.isError),
    ...next,
  };
  Object.assign(savedAiState, state);
  localStorage.setItem("pi-oj-ai", JSON.stringify(state));
}

function setAiOutput(message, isError = false, persist = true) {
  renderAiReadable(message, isError);
  if (persist) saveAiState({output: message || "", isError});
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

function parseAiExpectedOutput(message) {
  const fencePattern = new RegExp('(?:^|\\n)\\s*(?:expected\\s+stdout|expected\\s+output|stdout|output)\\s*:\\s*```(?:text)?\\s*\\n([\\s\\S]*?)```', 'i');
  const expectedMatch = message.match(fencePattern);
  if (!expectedMatch) return null;
  return expectedMatch[1].replace(/\n$/, "");
}

function shouldAttachProblemContextForAsk() {
  const question = $("aiQuestion").value.trim().toLowerCase();
  if (!question || !validProblemId($("problemId").value)) return false;
  return /(\bthis\b|current|problem|uva|codeforces|wa|wrong answer|tle|re|ce|debug|output|stdout|stdin|sample|test|case|code|\u9019\u984c|\u984c\u76ee|\u76ee\u524d|\u9019\u4efd|\u7a0b\u5f0f|\u7a0b\u5f0f\u78bc|\u6e2c\u8cc7|\u8f38\u5165|\u8f38\u51fa|\u932f|\u9664\u932f|\u70ba\u4ec0\u9ebc\u932f)/i.test(question);
}

async function requestAi(mode) {
  const result = lastRunResult || {};
  const isCustomAsk = mode === "ask";
  const attachProblem = !isCustomAsk || shouldAttachProblemContextForAsk();
  const queued = await request("/api/ai/help", {method: "POST", body: JSON.stringify({
    mode,
    judge: effectiveJudge(),
    problem_id: attachProblem ? $("problemId").value.trim() : "",
    language: $("language").value,
    code: attachProblem ? getCode() : "",
    input_data: attachProblem ? $("input").value : "",
    expected_output: attachProblem ? $("expected").value : "",
    stdout: attachProblem ? (result.stdout || "") : "",
    error_log: attachProblem ? (result.error_log || "") : "",
    verdict: attachProblem ? (result.verdict || result.status || "") : "",
    question: $("aiQuestion").value,
  })});
  return poll(queued.task_id);
}

function showAiResult(result) {
  const aiModeNames = {hint: "AI \u63d0\u793a", debug: "AI \u9664\u932f", translate: "AI \u7ffb\u8b6f", tests: "AI \u6e2c\u8cc7", expected: "AI \u8f38\u51fa\u8a08\u7b97", ask: "AI \u56de\u7b54"};
  const title = aiModeNames[result.mode] || "AI \u56de\u7b54";
  const meta = [result.provider, result.model].filter(Boolean).join(" / ");
  const parts = [`# ${title}${meta ? ` (${meta})` : ""}`];
  if (result.message) parts.push(result.message);
  if (result.error_log) parts.push(`error:\n\`\`\`text\n${result.error_log}\n\`\`\``);
  setAiOutput(parts.join("\n\n") || JSON.stringify(result, null, 2), result.status === "SE");
}

async function askAi(mode) {
  if (mode !== "ask" && !validProblemId($("problemId").value)) {
    toast(invalidProblemMessage() + "\uff0cAI \u52a9\u6559\u9700\u8981\u77e5\u9053\u662f\u54ea\u4e00\u984c");
    return;
  }
  if (mode === "ask" && !$("aiQuestion").value.trim()) {
    toast("\u8acb\u5148\u8f38\u5165\u60f3\u554f AI \u7684\u5167\u5bb9");
    $("aiQuestion").focus();
    return;
  }
  const buttons = {ask: $("aiAsk"), hint: $("aiHint"), debug: $("aiDebug"), translate: $("aiTranslate")};
  const button = buttons[mode] || $("aiAsk");
  button.disabled = true;
  const pageName = effectiveJudge() === "codeforces" ? "Codeforces \u984c\u76ee\u9801" : "UVA PDF";
  const waitingMessages = {
    ask: "AI \u52a9\u6559\u6b63\u5728\u56de\u7b54\u4f60\u7684\u554f\u984c...",
    translate: `AI \u52a9\u6559\u6b63\u5728\u8b80\u53d6 ${pageName} \u4e26\u7ffb\u8b6f\u984c\u76ee...`,
    hint: "AI \u52a9\u6559\u6b63\u5728\u95b1\u8b80\u984c\u865f\u8207\u7a0b\u5f0f\u78bc...",
    debug: "AI \u52a9\u6559\u6b63\u5728\u95b1\u8b80\u6e2c\u8a66\u7d50\u679c\u8207\u7a0b\u5f0f\u78bc...",
  };
  setAiOutput(waitingMessages[mode] || "AI \u52a9\u6559\u6b63\u5728\u601d\u8003...");

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
      if (!validProblemId($("problemId").value)) {
        throw new Error(invalidProblemMessage());
      }
      if (effectiveJudge() === "codeforces") {
        if (!confirm(`Codeforces \u6703\u64cb\u4f3a\u670d\u5668\u81ea\u52d5\u767b\u5165\u3002\u8981\u8907\u88fd\u7a0b\u5f0f\u78bc\u4e26\u958b\u555f Codeforces \u63d0\u4ea4\u9801\u55ce\uff1f`)) return;
        try {
          await navigator.clipboard.writeText(getCode());
          toast("\u5df2\u8907\u88fd\u7a0b\u5f0f\u78bc\uff0c\u8acb\u8cbc\u5230 Codeforces \u63d0\u4ea4\u9801");
        } catch (copyError) {
          toast("\u700f\u89bd\u5668\u4e0d\u5141\u8a31\u81ea\u52d5\u8907\u88fd\uff0c\u8acb\u624b\u52d5\u8907\u88fd\u7a0b\u5f0f\u78bc");
        }
        const problemCode = codeforcesProblemCode($("problemId").value);
        window.open(codeforcesSubmitUrl($("problemId").value), "_blank", "noopener,noreferrer");
        setVerdict("MANUAL");
        $("output").textContent = `Codeforces Cloudflare \u6703\u963b\u64cb worker \u81ea\u52d5\u63d0\u4ea4\u3002\n\u5df2\u958b\u555f\u63d0\u4ea4\u9801${problemCode ? `\uff0c\u984c\u865f\uff1a${problemCode}` : ""}\u3002\n\u8acb\u8cbc\u4e0a\u525b\u8907\u88fd\u7684\u7a0b\u5f0f\u78bc\u4e26\u624b\u52d5\u63d0\u4ea4\u3002`;
        return;
      }
      if (!confirm(`\u78ba\u5b9a\u8981\u63d0\u4ea4\u76ee\u524d\u7a0b\u5f0f\u78bc\u5230 UVA ${$("problemId").value}\uff1f`)) return;
      response = await request("/api/uva/submit", {method: "POST", body: JSON.stringify({
        judge: effectiveJudge(),
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
  saved[activeLanguage] = getCode();
  activeLanguage = normalizeLanguage(event.target.value, effectiveJudge());
  event.target.value = activeLanguage;
  localStorage.setItem(languageStorageKey(effectiveJudge()), activeLanguage);
  setCode(saved[activeLanguage] || templates[activeLanguage] || "");
  if (codeEditor && window.monaco) {
    monaco.editor.setModelLanguage(codeEditor.getModel(), currentLanguageMode());
  }
  clearDiagnostics();
});
$("testSource").addEventListener("change", (event) => switchTestSource(event.target.value));
$("judge").addEventListener("change", () => {
  saved[activeLanguage] = getCode();
  localStorage.setItem("pi-oj-judge", getJudge());
  renderLanguageOptions(effectiveJudge());
  setCode(saved[activeLanguage] || templates[activeLanguage] || "");
  if (codeEditor && window.monaco) monaco.editor.setModelLanguage(codeEditor.getModel(), currentLanguageMode());
  $("problemId").value = getJudge() === "cpe"
    ? localStorage.getItem("pi-oj-problem-id-cpe") || ""
    : localStorage.getItem(`pi-oj-problem-id-${getJudge()}`) || "";
  updateJudgeUi();
  switchTestSource($("testSource").value);
});
$("problemId").addEventListener("input", (event) => {
  localStorage.setItem(`pi-oj-problem-id-${getJudge()}`, event.target.value.trim());
});
$("cpeLevel").addEventListener("change", () => renderCpeStarOptions());
$("cpeStar").addEventListener("change", (event) => applyCpeStarProblem(event.target.value));
$("fetchTests").addEventListener("click", fetchSelectedTests);
$("input").addEventListener("input", saveActiveTest);
$("expected").addEventListener("input", saveActiveTest);
$("aiQuestion").addEventListener("input", () => saveAiState());
$("openUdebug").addEventListener("click", () => {
  const problemId = $("problemId").value;
  if (!validProblemId(problemId)) {
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
$("aiAsk").addEventListener("click", () => askAi("ask"));
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

if (savedAiState.output) {
  setAiOutput(savedAiState.output, Boolean(savedAiState.isError), false);
}

renderCpeStarOptions();
initializeMonaco();
const initialTestSource = ["sample", "udebug", "ai", "custom"].includes(localStorage.getItem("pi-oj-test-source")) ? localStorage.getItem("pi-oj-test-source") : "sample";
$("testSource").value = testSourceLabels[initialTestSource] ? initialTestSource : "sample";
syncTestSourceOptions();
switchTestSource($("testSource").value, false);
switchSidePanel(localStorage.getItem("pi-oj-side-panel") === "ai" ? "ai" : "test");
restoreRunResultState();

fetch("/api/health").then((r) => r.json()).then(() => {
  $("health").textContent = "Online";
  $("health").classList.add("ok");
}).catch(() => { $("health").textContent = "Offline"; });

