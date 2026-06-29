#!/usr/bin/env node

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:8000";

async function main() {
  let chromium;
  try {
    ({chromium} = require("playwright"));
  } catch {
    console.error("Playwright is not installed. Install it with: npm i -D playwright");
    process.exit(2);
  }

  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  const missing = [];
  page.on("response", (response) => {
    if (response.status() >= 400) missing.push(`${response.status()} ${response.url()}`);
  });

  const health = await fetch(`${baseUrl}/api/health`).then((res) => res.ok).catch(() => false);
  if (!health) throw new Error(`Health check failed: ${baseUrl}/api/health`);

  await page.goto(baseUrl, {waitUntil: "networkidle"});
  await page.waitForSelector("#judge", {timeout: 15000});
  await page.waitForSelector("#editor, #code", {timeout: 15000});
  await page.click("#aiTab");
  await page.waitForSelector("#aiPanel:not([hidden])", {timeout: 5000});
  await page.click("#testTab");
  await page.waitForSelector("#testPanel.active", {timeout: 5000});

  await page.goto(`${baseUrl}/static/cpe-exam.html`, {waitUntil: "networkidle"});
  await page.waitForSelector("#examMode", {state: "attached", timeout: 15000});
  await page.waitForSelector("#editor, #code", {timeout: 15000});

  const relevantMissing = missing.filter((item) => !item.includes("/favicon.ico"));
  if (relevantMissing.length) {
    throw new Error(`Missing resources:\n${relevantMissing.join("\n")}`);
  }

  await browser.close();
  console.log("E2E smoke test passed.");
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});

