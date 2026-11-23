// Records the README animations from the app's real React UI (Vite dev server + Tauri IPC mock).
// Usage: node tools/readme-gifs/record.mjs <base-url> <frames-dir> [scene ...]
// Writes <frames-dir>/<scene>/NNNN.png and frames.json; make_gif.py turns them into GIFs.
import { chromium } from "playwright";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const [base, outDir, ...only] = process.argv.slice(2);
if (!base || !outDir) {
  console.error("usage: node record.mjs <base-url> <frames-dir> [scene ...]");
  process.exit(2);
}
const MOCK = readFileSync(join(here, "mock-tauri.js"), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A visible pointer, since headless screenshots have none.
const POINTER = `
  window.addEventListener("DOMContentLoaded", () => {
    if (window !== window.top) return;
    const p = document.createElement("div");
    p.id = "__pointer";
    p.style.cssText = "position:fixed;left:-40px;top:-40px;width:22px;height:22px;z-index:2147483647;pointer-events:none;transition:transform 120ms";
    p.innerHTML = '<svg viewBox="0 0 22 22" width="22" height="22"><path d="M3 2 L3 18 L7.5 14 L10.5 20.5 L13 19.4 L10 13 L16 13 Z" fill="#111" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    document.body.appendChild(p);
    addEventListener("mousemove", (e) => { p.style.left = e.clientX - 3 + "px"; p.style.top = e.clientY - 2 + "px"; }, true);
    addEventListener("mousedown", () => (p.style.transform = "scale(0.85)"), true);
    addEventListener("mouseup", () => (p.style.transform = ""), true);
  });`;

function recorder(page, dir) {
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  const frames = [];
  let stop = false;
  const loop = (async () => {
    while (!stop) {
      const t = Date.now();
      const file = String(frames.length).padStart(4, "0") + ".png";
      writeFileSync(
        join(dir, file),
        await page.screenshot({ caret: "initial" }),
      );
      frames.push({ file, t });
      const spent = Date.now() - t;
      if (spent < 66) await sleep(66 - spent);
    }
  })();
  return async () => {
    stop = true;
    await loop;
    const end = Date.now();
    const out = frames.map((f, i) => ({
      file: f.file,
      ms: (frames[i + 1]?.t ?? end) - f.t,
    }));
    writeFileSync(join(dir, "frames.json"), JSON.stringify(out));
    return out.length;
  };
}

async function click(page, locator, pause = 250) {
  const box = await locator.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
    steps: 12,
  });
  await sleep(pause);
  await locator.click();
}

async function scrollTo(page, locator, block = "start") {
  await locator.evaluate(
    (el, block) => el.scrollIntoView({ behavior: "smooth", block }),
    block,
  );
  await sleep(700);
}

// ---- Overlay-driven scenes (demo.html + the real overlay page in an iframe) ----

async function overlayFrame(page) {
  for (let i = 0; i < 50; i++) {
    const f = page.frames().find((fr) => fr.url().includes("/src/overlay/"));
    if (
      f &&
      (await f
        .evaluate(() => !!document.querySelector("#root"))
        .catch(() => false))
    )
      return f;
    await sleep(100);
  }
  throw new Error("overlay frame not found");
}

const emit = (frame, event, payload) =>
  frame.evaluate(([e, p]) => window.__emit(e, p), [event, payload]);

async function micLevels(frame, on) {
  await frame.evaluate((on) => {
    clearInterval(window.__mic);
    if (!on) return window.__emit("mic-level", Array(16).fill(0));
    window.__mic = setInterval(() => {
      const loud = Math.random() > 0.15;
      window.__emit(
        "mic-level",
        Array.from({ length: 16 }, () =>
          loud ? 0.25 + Math.random() * 0.7 : Math.random() * 0.1,
        ),
      );
    }, 70);
  }, on);
}

async function startDictation(page, frame) {
  await page.evaluate(() => window.demo.follow());
  await emit(frame, "overlay-anchor", "top");
  await emit(frame, "show-overlay", "streaming");
  await sleep(550);
  await emit(frame, "recording-ready", null);
  await micLevels(frame, true);
  await sleep(350);
}

async function finishDictation(frame, kind, ms) {
  await micLevels(frame, false);
  await emit(frame, "stream-phase-event", { phase: "working", kind });
  await sleep(ms);
  await emit(frame, "hide-overlay", null);
}

async function speak(frame, committed, chunks, step) {
  let tentative = "";
  for (const c of chunks) {
    tentative += c;
    await emit(frame, "stream-text-event", { committed, tentative });
    await sleep(step);
  }
  return tentative;
}

const scenes = {
  // Live preview + sentence-by-sentence paste.
  async dictation(browser, dir) {
    const page = await browser.newPage({
      viewport: { width: 860, height: 380 },
      colorScheme: "light",
    });
    await page.addInitScript({ content: MOCK });
    await page.goto(base + "/tools/readme-gifs/demo.html");
    const frame = await overlayFrame(page);
    await page.evaluate(() =>
      window.demo.setup({
        title: "Notes",
        initial: "Hi team,\n\n",
        left: "Simulated dictation, rendered with Handy Remote's real overlay",
        right: "Live preview · Paste while speaking",
      }),
    );
    await sleep(800);
    const done = recorder(page, dir);
    await sleep(700);
    await startDictation(page, frame);
    const sentences = [
      "Quick update on the release.",
      "The installer is signed and the updater checks the signature.",
      "Let me know if anything looks off.",
    ];
    let committed = "";
    for (const [i, s] of sentences.entries()) {
      const words = s.split(" ").map((w, j) => (j ? " " : "") + w);
      const said = await speak(frame, committed, words, 150);
      committed = (committed ? committed + " " : "") + said;
      await emit(frame, "stream-text-event", { committed, tentative: "" });
      if (i < sentences.length - 1) {
        await page.evaluate((s) => window.demo.paste(s + " "), said);
        await sleep(350);
      }
    }
    await sleep(300);
    await finishDictation(frame, "transcribing", 450);
    await page.evaluate((s) => window.demo.paste(s), sentences.at(-1));
    await sleep(1800);
    const n = await done();
    await page.close();
    return n;
  },

  // Raw speech -> LLM "Polish & structure" (+ My rules) -> numbered list.
  async refine(browser, dir) {
    const page = await browser.newPage({
      viewport: { width: 860, height: 380 },
      colorScheme: "light",
    });
    await page.addInitScript({ content: MOCK });
    await page.goto(base + "/tools/readme-gifs/demo.html");
    const frame = await overlayFrame(page);
    await page.evaluate(() =>
      window.demo.setup({
        title: "Team chat",
        initial: "",
        left: "Simulated dictation, rendered with Handy Remote's real overlay",
        right: "Refine every dictation · Polish & structure + My rules",
      }),
    );
    await sleep(800);
    const done = recorder(page, dir);
    await sleep(600);
    await startDictation(page, frame);
    const raw =
      "嗯我说一下明天会议的安排第一点九点钟大家到三楼会议室集合第二点每个人准备五分钟的汇报第三点那个会后一起吃午饭";
    const chunks = raw.match(/.{1,2}/gu);
    await speak(frame, "", chunks, 95);
    await sleep(300);
    await finishDictation(frame, "polishing", 1500);
    await sleep(250);
    await page.evaluate(() =>
      window.demo.paste(
        "我说一下明天会议的安排：\n1. 9点钟大家到3楼会议室集合。\n2. 每个人准备五分钟的汇报。\n3. 会后一起吃午饭。",
      ),
    );
    await sleep(2100);
    const n = await done();
    await page.close();
    return n;
  },

  // Settings -> Cloud: enable, pick a provider, model and key, test, save.
  async cloud(browser, dir) {
    const page = await browser.newPage({
      viewport: { width: 880, height: 680 },
      colorScheme: "light",
    });
    await page.addInitScript({ content: MOCK });
    await page.addInitScript({ content: POINTER });
    await page.goto(base + "/");
    await page.getByText("Cloud", { exact: true }).first().click();
    await page.mouse.move(600, 560);
    await scrollTo(
      page,
      page.getByText("Recognition", { exact: true }).first(),
    );
    await sleep(300);
    const done = recorder(page, dir);
    await sleep(500);
    const toggle = page.getByLabel("Use cloud recognition").locator("xpath=..");
    await click(page, toggle);
    await sleep(500);

    const trigger = page.getByRole("button", { name: "OpenAI", exact: true });
    await click(page, trigger);
    await sleep(500);
    const menu = trigger.locator("xpath=following-sibling::div[1]");
    await menu.evaluate((el) =>
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" }),
    );
    await sleep(700);
    await menu.evaluate((el) => el.scrollTo({ top: 0, behavior: "smooth" }));
    await sleep(400);
    await click(page, menu.getByRole("button", { name: "Deepgram" }));
    await sleep(400);
    const model = page.getByPlaceholder("nova-3");
    await click(page, model, 150);
    await model.pressSequentially("nova-3", { delay: 40 });
    await sleep(200);
    const key = page.getByPlaceholder("Paste the key");
    await click(page, key, 150);
    await key.pressSequentially("sk-example-0000000000000000", { delay: 15 });
    await sleep(300);
    const test = page.getByRole("button", { name: "Test", exact: true });
    await click(page, test);
    await sleep(1500);
    await click(page, page.getByRole("button", { name: "Save", exact: true }));
    await sleep(1500);
    const n = await done();
    await page.close();
    return n;
  },

  // Settings -> Post Process: find local models (Copilot bridge), speed test, My rules.
  async "refine-settings"(browser, dir) {
    const page = await browser.newPage({
      viewport: { width: 880, height: 600 },
      colorScheme: "light",
    });
    await page.addInitScript({ content: MOCK });
    await page.addInitScript({ content: POINTER });
    await page.goto(base + "/");
    await page.getByText("Post Process", { exact: true }).first().click();
    await page.mouse.move(600, 560);
    await scrollTo(
      page,
      page.getByText("Refine with a language model", { exact: true }).first(),
    );
    await sleep(500);
    const done = recorder(page, dir);
    await sleep(500);
    await click(page, page.getByRole("button", { name: "Find local models" }));
    await sleep(1300);
    await click(page, page.getByRole("button", { name: "Run speed test" }));
    await sleep(2000);
    const rules = page.getByPlaceholder(/One rule per line/);
    await scrollTo(page, rules, "center");
    await rules.evaluate((el) => (el.spellcheck = false));
    await click(page, rules, 150);
    await rules.pressSequentially(
      '楼层用阿拉伯数字（三楼→3楼），数量保留汉字（三个问题、五分钟）\nKeep my word order\nWrite "config点json" as config.json',
      { delay: 8 },
    );
    await sleep(300);
    await click(page, page.getByRole("button", { name: "Save rules" }));
    await sleep(1500);
    const n = await done();
    await page.close();
    return n;
  },
};

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  for (const name of only.length ? only : Object.keys(scenes)) {
    const n = await scenes[name](browser, join(outDir, name));
    console.log(`${name}: ${n} frames`);
  }
} finally {
  await browser.close();
}
