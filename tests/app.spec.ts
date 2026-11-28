import { test, expect } from "@playwright/test";

test.describe("Live transcript recovery", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      let nextId = 0;
      const callbacks = new Map<number, (event: unknown) => void>();
      const listeners = new Map<number, { event: string; handler: number }>();
      const host = {
        copied: [] as string[],
        failCopy: false,
        dismissed: 0,
        stopped: 0,
        failStop: false,
        emit(event: string, payload: unknown) {
          for (const [id, listener] of listeners) {
            if (listener.event === event)
              callbacks.get(listener.handler)?.({ event, id, payload });
          }
        },
        listenerCount: (event: string) =>
          [...listeners.values()].filter((l) => l.event === event).length,
      };
      Object.assign(window, {
        overlayTest: host,
        __TAURI_EVENT_PLUGIN_INTERNALS__: {
          unregisterListener: (_event: string, id: number) =>
            listeners.delete(id),
        },
        __TAURI_INTERNALS__: {
          transformCallback(callback: (event: unknown) => void) {
            const id = ++nextId;
            callbacks.set(id, callback);
            return id;
          },
          async invoke(command: string, args: Record<string, unknown> = {}) {
            if (command === "plugin:event|listen") {
              const id = ++nextId;
              listeners.set(id, {
                event: String(args.event),
                handler: Number(args.handler),
              });
              return id;
            }
            if (command === "plugin:event|unlisten") {
              listeners.delete(Number(args.eventId));
              return;
            }
            if (command === "get_app_settings")
              return {
                app_language: "en",
                overlay_position: "bottom",
                theme: "light",
              };
            if (command === "plugin:clipboard-manager|write_text") {
              if (host.failCopy) throw new Error("clipboard busy");
              host.copied.push(String(args.text));
              return;
            }
            if (command === "dismiss_overlay_notice") {
              host.dismissed += 1;
              return;
            }
            if (command === "stop_recording") {
              host.stopped += 1;
              if (host.failStop) throw "coordinator unavailable";
              return;
            }
            if (command === "plugin:os|locale") return "en-US";
            throw new Error(`Unexpected command: ${command}`);
          },
        },
      });
    });
    await page.goto("/src/overlay/index.html");
    await expect
      .poll(() =>
        page.evaluate(() => {
          return Reflect.get(window, "overlayTest").listenerCount(
            "stream-text-event",
          );
        }),
      )
      .toBe(1);
    await page.evaluate(() =>
      Reflect.get(window, "overlayTest").emit("show-overlay", "streaming"),
    );
    await expect(page.locator(".scard")).toBeVisible();
  });

  for (const style of ["streaming", "recording"]) {
    test(`microphone finishes ${style} without focusing or starting another take`, async ({
      page,
    }) => {
      await page.setViewportSize(
        style === "streaming"
          ? { width: 400, height: 120 }
          : { width: 266, height: 50 },
      );
      await page.evaluate((style) => {
        const host = Reflect.get(window, "overlayTest");
        if (style === "recording") host.emit("show-overlay", style);
        else {
          host.emit("recording-ready", null);
          host.emit("stream-text-event", {
            committed: "Keep this text",
            tentative: "",
          });
        }
        const target = document.createElement("input");
        target.id = "paste-target";
        document.body.prepend(target);
        target.focus();
      }, style);
      if (style === "recording")
        await expect(page.locator(".scard.compact")).toBeVisible();
      const stop = page.getByRole("button", { name: "Finish recording" });
      await expect(stop).toBeEnabled();
      // The former ready-cue microphone faded out after 2.2 seconds.
      if (style === "streaming") await page.waitForTimeout(2300);
      await expect(stop).toHaveCSS("opacity", "1");
      await stop.click();
      await expect(stop).toBeDisabled();
      await expect(page.locator("#paste-target")).toBeFocused();
      await stop.evaluate((button: HTMLButtonElement) => button.click());
      expect(
        await page.evaluate(() => Reflect.get(window, "overlayTest").stopped),
      ).toBe(1);
      await page.evaluate((style) => {
        const host = Reflect.get(window, "overlayTest");
        if (style === "streaming")
          host.emit("stream-phase-event", {
            phase: "working",
            kind: "transcribing",
          });
        else host.emit("show-overlay", "transcribing");
      }, style);
      await expect(stop).toHaveCount(0);
      if (style === "streaming")
        await expect(page.locator(".committed")).toHaveText("Keep this text");
      await page.evaluate((style) => {
        const host = Reflect.get(window, "overlayTest");
        host.emit("hide-overlay", null);
        host.emit("show-overlay", style);
      }, style);
      await expect(stop).toBeEnabled();
      await stop.click();
      expect(
        await page.evaluate(() => Reflect.get(window, "overlayTest").stopped),
      ).toBe(2);
    });
  }

  for (const style of ["streaming", "recording"]) {
    test(`failed microphone stop in ${style} can be retried`, async ({
      page,
    }) => {
      await page.setViewportSize(
        style === "streaming"
          ? { width: 400, height: 120 }
          : { width: 266, height: 50 },
      );
      await page.evaluate((style) => {
        const host = Reflect.get(window, "overlayTest");
        if (style === "recording") host.emit("show-overlay", style);
        else
          host.emit("stream-text-event", {
            committed: "Keep this text",
            tentative: " and preview",
          });
        host.failStop = true;
      }, style);
      if (style === "recording")
        await expect(page.locator(".scard.compact")).toBeVisible();
      const stop = page.getByRole("button", { name: "Finish recording" });
      await stop.click();
      await expect(page.getByRole("alert")).toHaveText(
        "Could not finish recording. Click the microphone to retry.",
      );
      await expect(page.getByRole("alert")).toBeInViewport({ ratio: 1 });
      await expect(stop).toBeEnabled();
      if (style === "streaming") {
        await expect(page.locator(".tentative")).toHaveText(" and preview");
        await page
          .getByRole("button", { name: "Copy transcription to clipboard" })
          .click();
        expect(
          await page.evaluate(() => Reflect.get(window, "overlayTest").copied),
        ).toEqual(["Keep this text and preview"]);
      }
      await page.evaluate(() => {
        Reflect.get(window, "overlayTest").failStop = false;
      });
      await stop.click();
      await expect(page.getByRole("alert")).toHaveCount(0);
      await expect(stop).toBeDisabled();
      expect(
        await page.evaluate(() => Reflect.get(window, "overlayTest").stopped),
      ).toBe(2);
    });
  }

  test("copies committed plus tentative text during recording, finalization and failure", async ({
    page,
  }) => {
    const text = {
      committed: "First sentence.\n".repeat(30),
      tentative: " Still speaking \u4e2d\u6587",
    };
    await page.evaluate(
      (payload) =>
        Reflect.get(window, "overlayTest").emit("stream-text-event", payload),
      text,
    );
    const copy = page.getByRole("button", {
      name: "Copy transcription to clipboard",
    });
    for (const phase of ["listening", "working", "polishing", "failed"]) {
      await page.evaluate((phase) => {
        const host = Reflect.get(window, "overlayTest");
        if (phase === "failed") {
          host.emit("overlay-notice", {
            code: "TranscriptionFailed",
            level: "error",
            source: "dictation",
          });
          host.emit("show-overlay", "live_notice");
        } else if (phase === "polishing") {
          host.emit("stream-phase-event", {
            phase: "working",
            kind: "polishing",
          });
        } else host.emit("stream-phase-event", { phase, kind: "transcribing" });
      }, phase);
      if (phase === "failed")
        await expect(page.locator(".cnotice")).toBeVisible();
      await copy.click();
      await expect(page.getByRole("status")).toHaveText("Copied");
    }
    expect(
      await page.evaluate(() => Reflect.get(window, "overlayTest").copied),
    ).toEqual(Array(4).fill(text.committed + text.tentative));
    await page.locator(".stext-cap").click();
    expect(
      await page.evaluate(() => Reflect.get(window, "overlayTest").dismissed),
    ).toBe(0);
    await expect(copy).toBeVisible();
  });

  test("copy errors remain visible, preserve text and can be retried", async ({
    page,
  }) => {
    await page.evaluate(() => {
      const host = Reflect.get(window, "overlayTest");
      host.emit("stream-text-event", {
        committed: "",
        tentative: "Preview to rescue",
      });
      host.failCopy = true;
    });
    const copy = page.getByRole("button", {
      name: "Copy transcription to clipboard",
    });
    await copy.click();
    await expect(page.getByRole("status")).toHaveText(
      "Failed to copy transcription to clipboard.",
    );
    await expect(page.locator(".tentative")).toHaveText("Preview to rescue");
    await page.evaluate(() => {
      Reflect.get(window, "overlayTest").failCopy = false;
    });
    await copy.click();
    await expect(page.getByRole("status")).toHaveText("Copied");
  });

  test("a new dictation cancels stale hide cleanup and never copies the previous take", async ({
    page,
  }) => {
    await page.evaluate(() => {
      const host = Reflect.get(window, "overlayTest");
      host.emit("stream-text-event", { committed: "Old take", tentative: "" });
      host.emit("hide-overlay", null);
      host.emit("show-overlay", "streaming");
      host.emit("stream-text-event", { committed: "", tentative: "New take" });
    });
    await expect(page.locator(".tentative")).toHaveText("New take");
    await page.waitForTimeout(350);
    await page
      .getByRole("button", { name: "Copy transcription to clipboard" })
      .click();
    expect(
      await page.evaluate(() => Reflect.get(window, "overlayTest").copied),
    ).toEqual(["New take"]);
  });
});

test.describe("Handy App", () => {
  test("dev server responds", async ({ page }) => {
    // Just verify the dev server is running and responds
    const response = await page.goto("/");
    expect(response?.status()).toBe(200);
  });

  test("page has html structure", async ({ page }) => {
    await page.goto("/");

    // Verify basic HTML structure exists
    const html = await page.content();
    expect(html).toContain("<html");
    expect(html).toContain("<body");
  });
});
