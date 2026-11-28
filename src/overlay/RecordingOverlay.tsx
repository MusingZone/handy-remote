import { listen } from "@tauri-apps/api/event";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { Check, Copy } from "lucide-react";
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import "./RecordingOverlay.css";
import { commands, events } from "@/bindings";
import type {
  Notice,
  StreamPhase,
  StreamPhaseEvent,
  StreamTextEvent,
  StreamWorkKind,
} from "@/bindings";
import { NoticeBadgeIcon } from "@/components/icons/NoticeBadgeIcon";
import i18n, { syncLanguageFromSettings } from "@/i18n";
import { noticeDisplay } from "@/lib/notices";
import type { ModelStateEvent } from "@/lib/types/events";
import { getLanguageDirection } from "@/lib/utils/rtl";

type OverlayState =
  | "recording"
  | "streaming"
  | "transcribing"
  | "processing"
  | "notice"
  | "live_notice";

const isNoticeState = (state: OverlayState) =>
  state === "notice" || state === "live_notice";

// Number of reactive bars in the waveform (the simple, smoothed style shared by
// every overlay form). Mic levels arrive as 16 FFT buckets; we take the first N.
const WAVE_BARS = 9;

// Only call out a model load in the Live preview once it has run this long.
// Warm loads finish in well under this (~0.2s on Apple Silicon, ~1.5s on a
// Windows CPU backend), so the common case never flashes a loading notice while
// the user is speaking; cold loads can take 40s+.
const SLOW_MODEL_LOAD_MS = 2000;

const REMOTE_WAKING = "{{handy-remote:waking}}";

// Matches the delay in `hide_recording_overlay`, so a notice can fade out.
const OVERLAY_FADE_MS = 300;

// 13px padding on each side, the 16px badge and its 7px gap.
const NOTICE_PILL_CHROME = 49;

const RecordingOverlay: React.FC = () => {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [state, setState] = useState<OverlayState>("recording");
  // `Stream::play()` returning does not mean hardware callbacks are flowing.
  // Stay visually in an arming state until the backend processes the first
  // actual microphone sample chunk.
  const [captureReady, setCaptureReady] = useState(false);
  // Fork: whether this take goes through the LLM, and the user's per-take switch.
  const [refines, setRefines] = useState(false);
  const [refineOff, setRefineOff] = useState(false);
  // Recording starts while the model loads in the background. Say so, otherwise
  // a cold start looks like an empty Live preview that ignores speech, then a
  // hung "Transcribing..." spinner. Once recording stops, any in-flight load is
  // the wait, so the working label reflects it immediately; the Live notice
  // waits for the load to be slow so fast loads don't flash it mid-speech.
  const [modelLoading, setModelLoading] = useState(false);
  const [modelLoadSlow, setModelLoadSlow] = useState(false);
  const modelLoadTimerRef = useRef<ReturnType<typeof setTimeout>>();
  // Latched per Live session: once the loading notice has opened the panel, keep
  // it open after the load so it doesn't collapse and reopen when text arrives.
  const [loadNoticeShown, setLoadNoticeShown] = useState(false);
  const [levels, setLevels] = useState<number[]>(Array(WAVE_BARS).fill(0));
  const [streamText, setStreamText] = useState<StreamTextEvent>({
    committed: "",
    tentative: "",
  });
  const [phase, setPhase] = useState<StreamPhase>("listening");
  const [workKind, setWorkKind] = useState<StreamWorkKind>("transcribing");
  const [elapsed, setElapsed] = useState(0);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );
  const copyTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const [stopPending, setStopPending] = useState(false);
  const [stopFailed, setStopFailed] = useState(false);
  const stopRequestRef = useRef<object | null>(null);
  // Bumped on each new streaming session so the Live card remounts fresh (replays
  // the pop-in, and never animates in from the previous panel's open size).
  const [session, setSession] = useState(0);
  // Overlay placement (top vs bottom of the screen). The Live panel grows downward
  // from a top overlay (oldest line under the pill) and upward from a bottom one.
  const [position, setPosition] = useState<"top" | "bottom">("bottom");
  // True once live text overflows the cap. A top overlay fades its top edge only
  // while overflowing, so the resting first line stays crisp flush under the pill.
  const [overflowing, setOverflowing] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [noticeWidth, setNoticeWidth] = useState<number>();
  const noticeProbeRef = useRef<HTMLSpanElement>(null);
  const fadeTimerRef = useRef<ReturnType<typeof setTimeout>>();

  const smoothedLevelsRef = useRef<number[]>(Array(16).fill(0));
  // Live-text scroll-back: the text region "sticks" to the newest line while the
  // user is at the bottom; if they scroll up to read history, auto-follow pauses
  // until they scroll back down.
  const capRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  // Fork: set once native code reports the anchor edge; then settings no longer decide the layout.
  const anchoredRef = useRef(false);
  const direction = getLanguageDirection(i18n.language);

  useEffect(() => {
    let disposed = false;
    let generation = 0;
    let cleanup: (() => void) | undefined;
    const setupEventListeners = async () => {
      const unlistenNotice = await listen<Notice>("overlay-notice", (event) => {
        clearTimeout(fadeTimerRef.current);
        setNotice(event.payload);
      });

      // Fork: native placement (caret-following on Windows) says which edge the card hangs from.
      const unlistenAnchor = await listen<string>("overlay-anchor", (event) => {
        setPosition(event.payload === "top" ? "top" : "bottom");
        anchoredRef.current = true;
      });

      const unlistenShow = await listen("show-overlay", async (event) => {
        const shownGeneration = ++generation;
        const overlayState = event.payload as OverlayState;
        clearTimeout(fadeTimerRef.current);
        if (!isNoticeState(overlayState)) setNotice(null);
        // Reset synchronously before settings I/O. A fast microphone can emit
        // recording-ready while the awaits below are in flight; resetting after
        // them would overwrite that event and leave the overlay stuck arming.
        if (overlayState === "recording" || overlayState === "streaming") {
          setCaptureReady(false);
          smoothedLevelsRef.current = Array(16).fill(0);
          setLevels(Array(WAVE_BARS).fill(0));
          setStreamText({ committed: "", tentative: "" });
          setCopyState("idle");
          clearTimeout(copyTimerRef.current);
          stopRequestRef.current = null;
          setStopPending(false);
          setStopFailed(false);
        }

        await syncLanguageFromSettings();
        // The Live panel flows downward from a top overlay and upward from a
        // bottom one; read the placement so the layout can flip to match.
        try {
          const settings = await commands.getAppSettings();
          if (settings.status === "ok" && !anchoredRef.current) {
            setPosition(
              settings.data.overlay_position === "top" ? "top" : "bottom",
            );
          }
        } catch {
          // Keep the previous/default placement if settings can't be read.
        }
        if (disposed || generation !== shownGeneration) return;
        setState(overlayState);
        if (overlayState === "streaming") {
          setPhase("listening");
          setWorkKind("transcribing");
          setElapsed(0);
          setLoadNoticeShown(false);
          setSession((s) => s + 1); // remount the card fresh for this session
        }
        setIsVisible(true);
      });

      const unlistenHide = await listen("hide-overlay", () => {
        generation += 1;
        setIsVisible(false);
        setCaptureReady(false);
        // A notice keeps rendering while it fades. Dropping the transcript here
        // stops a later notice from showing an old session's text.
        clearTimeout(fadeTimerRef.current);
        clearTimeout(copyTimerRef.current);
        fadeTimerRef.current = setTimeout(() => {
          setNotice(null);
          setStreamText({ committed: "", tentative: "" });
        }, OVERLAY_FADE_MS);
      });

      const unlistenReady = await listen("recording-ready", () => {
        setElapsed(0);
        setCaptureReady(true);
      });

      // Sent at the start of every take, before or after show-overlay.
      const unlistenRefine = await listen<boolean>(
        "overlay-refine",
        (event) => {
          setRefines(event.payload);
          setRefineOff(false);
        },
      );

      const unlistenLevel = await listen<number[]>("mic-level", (event) => {
        // Levels only flow once the microphone delivers samples, so they also
        // repair a lost or reordered recording-ready event (seen once on a
        // Remote Desktop session: the card stayed dimmed while recording).
        setCaptureReady(true);
        const newLevels = event.payload as number[];
        // Exponential smoothing across the 16 buckets, then take the first N
        // bars for the shared waveform.
        const smoothed = smoothedLevelsRef.current.map((prev, i) => {
          const target = newLevels[i] || 0;
          return prev * 0.7 + target * 0.3;
        });
        smoothedLevelsRef.current = smoothed;
        setLevels(smoothed.slice(0, WAVE_BARS));
      });

      const unlistenStream = await events.streamTextEvent.listen((event) => {
        // Fork: a Handy Remote server sends status tokens in place of the preview.
        const status = (s: string) =>
          s.split(REMOTE_WAKING).join(i18n.t("overlay.remoteWaking"));
        setStreamText({
          committed: status(event.payload.committed),
          tentative: status(event.payload.tentative),
        });
      });

      const unlistenPhase = await events.streamPhaseEvent.listen((event) => {
        const payload: StreamPhaseEvent = event.payload;
        setPhase(payload.phase);
        if (payload.kind) setWorkKind(payload.kind);
      });

      // The backend ends every `loading_started` with exactly one of completed
      // or failed, so only those end a load. Other events (e.g. `unloaded` from
      // the idle watcher) aren't ordered against an in-flight load and must not
      // clear it.
      const unlistenModel = await listen<ModelStateEvent>(
        "model-state-changed",
        (event) => {
          const type = event.payload.event_type;
          if (type === "loading_started") {
            clearTimeout(modelLoadTimerRef.current);
            setModelLoading(true);
            setModelLoadSlow(false);
            modelLoadTimerRef.current = setTimeout(
              () => setModelLoadSlow(true),
              SLOW_MODEL_LOAD_MS,
            );
          } else if (
            type === "loading_completed" ||
            type === "loading_failed"
          ) {
            clearTimeout(modelLoadTimerRef.current);
            setModelLoading(false);
            setModelLoadSlow(false);
          }
        },
      );

      return () => {
        unlistenNotice();
        unlistenAnchor();
        unlistenShow();
        unlistenHide();
        unlistenReady();
        unlistenRefine();
        unlistenLevel();
        unlistenStream();
        unlistenPhase();
        unlistenModel();
        clearTimeout(modelLoadTimerRef.current);
        clearTimeout(fadeTimerRef.current);
        clearTimeout(copyTimerRef.current);
      };
    };

    setupEventListeners()
      .then((unlisten) => {
        if (disposed) unlisten();
        else cleanup = unlisten;
      })
      .catch((error) =>
        console.error("Failed to listen for overlay updates:", error),
      );
    return () => {
      disposed = true;
      generation += 1;
      cleanup?.();
    };
  }, []);

  // Elapsed capture timer starts only once microphone samples are flowing.
  useEffect(() => {
    if (state !== "streaming" || !isVisible || !captureReady) return;
    const id = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(id);
  }, [state, isVisible, captureReady]);

  // Stick to the bottom as text streams in — but only while pinned, so a user who
  // has scrolled up to read history isn't yanked back down by the next chunk.
  useLayoutEffect(() => {
    const el = capRef.current;
    if (!el) return;
    // Fade the top edge only once text actually overflows the cap.
    setOverflowing(el.scrollHeight > el.clientHeight + 1);
    if (pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [streamText]);

  // `session` re-runs this when a new Live session starts mid-load (e.g. a
  // retry during a cold load), since the reset doesn't change the other deps.
  useEffect(() => {
    if (modelLoadSlow && state === "streaming") setLoadNoticeShown(true);
  }, [modelLoadSlow, state, session]);

  // Each fresh streaming session starts pinned to the bottom, fade cleared.
  useEffect(() => {
    pinnedRef.current = true;
    setOverflowing(false);
  }, [session]);

  const shownNotice = notice && isNoticeState(state) ? notice : null;
  const noticeTitle = shownNotice
    ? t(noticeDisplay(shownNotice.code).title)
    : "";

  // CSS can't animate to an intrinsic width, so a notice pill gets a measured one.
  useLayoutEffect(() => {
    const probe = noticeProbeRef.current;
    if (!probe) return;
    setNoticeWidth(
      Math.ceil(probe.getBoundingClientRect().width) + NOTICE_PILL_CHROME,
    );
  }, [noticeTitle, isVisible]);

  if (!isVisible && !shownNotice) return null;

  // Re-pin when the user is within ~a line of the bottom; unpin otherwise.
  const handleStreamScroll = () => {
    const el = capRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 16;
  };

  const fmtTime = (s: number) =>
    `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  const transcribingLabel = modelLoading
    ? t("overlay.loadingModel")
    : t("overlay.transcribing");

  // ---- Shared building blocks (one visual language for every overlay form) ----
  const waveform = (
    <div className={`swave ${captureReady ? "ready" : "arming"}`}>
      {levels.map((v, i) => (
        <i
          key={i}
          style={{
            height: `${Math.max(3, Math.min(18, 3 + Math.pow(v, 0.7) * 15))}px`,
          }}
        />
      ))}
    </div>
  );

  const cancelBtn = (
    <button
      className="sx"
      aria-label="cancel"
      onClick={() => commands.cancelOperation()}
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path
          d="M4 4 L12 12 M12 4 L4 12"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
        />
      </svg>
    </button>
  );

  const copyBtn = (streamText.committed || streamText.tentative) && (
    <button
      className="sx"
      aria-label={t("settings.history.copyToClipboard")}
      title={t("settings.history.copyToClipboard")}
      onMouseDown={(e) => e.preventDefault()}
      onClick={async (e) => {
        e.stopPropagation();
        const text = streamText.committed + streamText.tentative;
        try {
          await writeText(text);
          setCopyState("copied");
        } catch (error) {
          console.error("Failed to copy live transcription:", error);
          setCopyState("failed");
        }
        clearTimeout(copyTimerRef.current);
        copyTimerRef.current = setTimeout(() => setCopyState("idle"), 2000);
      }}
    >
      {copyState === "copied" ? (
        <Check aria-hidden="true" />
      ) : (
        <Copy aria-hidden="true" />
      )}
    </button>
  );

  const copyStatus = copyState !== "idle" && (
    <span
      className="scopy-status"
      role="status"
      onClick={(e) => e.stopPropagation()}
    >
      {t(
        copyState === "copied"
          ? "settings.debug.liveLogs.copied"
          : "settings.history.copyError",
      )}
    </span>
  );

  const stopStatus = stopFailed && (
    <span
      className="swork-label sstop-error"
      role="alert"
      title={t("overlay.stopFailed")}
    >
      {t("overlay.stopFailed")}
    </span>
  );

  const stopRecording = async (e: React.MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (stopRequestRef.current) return;
    const request = {};
    stopRequestRef.current = request;
    setStopPending(true);
    setStopFailed(false);
    try {
      const result = await commands.stopRecording();
      if (result.status === "error") throw new Error(result.error);
    } catch (error) {
      console.error("Failed to stop recording from overlay:", error);
      if (stopRequestRef.current !== request) return;
      stopRequestRef.current = null;
      setStopPending(false);
      setStopFailed(true);
    }
  };

  // Fork: per-take refine switch. While recording it toggles; once the take is
  // being processed it can only turn refining off (the reply is then abandoned).
  const refineBtn = (working: boolean) => {
    if (!refines) return null;
    const locked = working && refineOff;
    return (
      <button
        className={`srefine ${refineOff ? "off" : "on"}`}
        aria-label={t(refineOff ? "overlay.refineOff" : "overlay.refineOn")}
        aria-pressed={!refineOff}
        title={t(refineOff ? "overlay.refineOff" : "overlay.refineOn")}
        disabled={locked}
        onClick={(e) => {
          e.stopPropagation();
          const next = working ? true : !refineOff;
          setRefineOff(next);
          commands.setRefineSkipped(next);
        }}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path
            className="star"
            d="M7 1.8 C7.4 5 8.2 5.9 11.6 6.4 C8.2 6.9 7.4 7.8 7 11 C6.6 7.8 5.8 6.9 2.4 6.4 C5.8 5.9 6.6 5 7 1.8 Z"
            fill="currentColor"
          />
          <path
            className="star small"
            d="M12.2 9.6 C12.4 11 12.8 11.4 14.2 11.6 C12.8 11.8 12.4 12.2 12.2 13.6 C12 12.2 11.6 11.8 10.2 11.6 C11.6 11.4 12 11 12.2 9.6 Z"
            fill="currentColor"
          />
          <path
            className="slash"
            d="M2.5 13.5 L13.5 2.5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            fill="none"
          />
        </svg>
      </button>
    );
  };

  // dot (left) | waveform (center) | timer + cancel (right) — same structure for
  // pill & panel, so the Live morph is a pure width change.
  const listeningRow = (showTimer: boolean, showCancel: boolean) => (
    <div className="sbase">
      <div className="sbase-l">
        <span
          className={`sdot ${captureReady ? "ready" : "arming"}`}
          role={captureReady ? "status" : undefined}
          aria-label={captureReady ? t("overlay.speakNow") : undefined}
        />
        <button
          type="button"
          className={`scue sstop ${captureReady ? "ready" : "arming"}`}
          aria-label={t("overlay.stopRecording")}
          title={t("overlay.stopRecording")}
          disabled={stopPending}
          onMouseDown={(e) => e.preventDefault()}
          onClick={stopRecording}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <rect
              x="5.5"
              y="1.5"
              width="5"
              height="8.5"
              rx="2.5"
              fill="currentColor"
            />
            <path
              d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>
      {stopStatus || waveform}
      <div className="sbase-r">
        {showTimer && <span className="stimer">{fmtTime(elapsed)}</span>}
        {copyBtn}
        {refineBtn(false)}
        {showCancel && cancelBtn}
      </div>
    </div>
  );

  // spinner (left) | label (center) | cancel (right) — same 3-zone grid as the
  // listening row, so the label is centered.
  const workingRow = (label: string, showCancel: boolean) => (
    <div className="sbase">
      <div className="sbase-l">
        <span className="sspinner" />
      </div>
      <span className="swork-label">{label}</span>
      <div className="sbase-r">
        {copyBtn}
        {refineBtn(true)}
        {showCancel && cancelBtn}
      </div>
    </div>
  );

  const noticeRow = (
    <div className="sbase snotice">
      <NoticeBadgeIcon className="nglyph" />
      <span className="nlabel">{noticeTitle}</span>
    </div>
  );
  const noticeProbe = shownNotice && (
    <span ref={noticeProbeRef} className="nlabel nprobe" aria-hidden="true">
      {noticeTitle}
    </span>
  );
  const noticeClass = shownNotice ? `cnotice level-${shownNotice.level}` : "";
  const dismissNotice = shownNotice
    ? () => commands.dismissOverlayNotice()
    : undefined;

  // ---- Live overlay: a pill that sculpts open into a panel ----
  const liveNotice = state === "live_notice" ? shownNotice : null;
  if (state === "streaming" || liveNotice) {
    const hasText =
      streamText.committed.length > 0 || streamText.tentative.length > 0;
    const working = !liveNotice && phase === "working";
    // A notice keeps the transcript; with none, the panel explains the notice.
    const noticeText =
      liveNotice && !hasText
        ? (noticeDisplay(liveNotice.code).explanation?.(t) ?? liveNotice.detail)
        : null;
    // While listening, a slow model load opens the panel with a notice where the
    // text would be, so speech that isn't transcribing yet doesn't look ignored.
    const loadingNotice = !liveNotice && modelLoadSlow && !working && !hasText;
    // Keep the panel open whenever there's text — even while finalizing — so the
    // transcript stays put under a working spinner instead of collapsing and
    // squishing the text mid-stream. Only fall back to the small working pill
    // when there was no text to preserve.
    const open = liveNotice
      ? hasText || !!noticeText
      : hasText || loadingNotice || (loadNoticeShown && !working);
    const collapsed = working && !hasText;
    // mic warm-up cue: dim until the first mic samples arrive, then glow once.
    const readyCls =
      working || liveNotice ? "" : captureReady ? "lit" : "arming";

    return (
      <div dir={direction} className={`ov-stage ${position}`}>
        {noticeProbe}
        <div
          key={session}
          className={`scard ${open ? "open" : ""} ${collapsed ? "working" : ""} ${readyCls} ${
            isVisible ? "" : "leaving"
          } ${refines && !liveNotice ? "has-refine" : ""} ${noticeClass}`}
          style={liveNotice && !open ? { width: noticeWidth } : undefined}
          onClick={dismissNotice}
        >
          <div className="stext">
            <div className="stext-clip">
              <div
                className={`stext-cap ${overflowing ? "overflowing" : ""}`}
                ref={capRef}
                onScroll={handleStreamScroll}
                onClick={(e) => e.stopPropagation()}
                onMouseDown={() => {
                  pinnedRef.current = false;
                }}
              >
                <p>
                  {loadingNotice && (
                    <span className="sloading">
                      {t("overlay.loadingModel")}
                    </span>
                  )}
                  {noticeText && <span className="ndetail">{noticeText}</span>}
                  <span className="committed">
                    {streamText.committed ? streamText.committed + " " : ""}
                  </span>
                  <span className="tentative">{streamText.tentative}</span>
                  {/* Drop the blinking caret once finalizing — it's no longer
                      capturing, and a static spinner conveys the work. */}
                  {!working && !loadingNotice && !liveNotice && (
                    <span className="scaret" />
                  )}
                </p>
              </div>
            </div>
          </div>
          {copyStatus}
          {liveNotice ? (
            <div className="sbase">
              <NoticeBadgeIcon className="nglyph" />
              <span className="nlabel">{noticeTitle}</span>
              <div className="sbase-r">{copyBtn}</div>
            </div>
          ) : working ? (
            workingRow(
              workKind === "polishing"
                ? t("overlay.processing")
                : transcribingLabel,
              true,
            )
          ) : (
            listeningRow(open, true)
          )}
        </div>
      </div>
    );
  }

  // ---- Minimal overlay: exactly one row at a time — waveform (recording), or a
  // spinner + label (transcribing / processing). Never both. The pill animates its
  // width between them; the cancel button is in both rows so it stays put.
  const working = state === "transcribing" || state === "processing";
  const workLabel =
    state === "processing" ? t("overlay.processing") : transcribingLabel;

  return (
    <div
      dir={direction}
      className={`ov-stage ${position} ov-fade ${isVisible ? "show" : ""}`}
    >
      {noticeProbe}
      <div
        className={`scard compact ${working && isVisible ? "cworking" : ""} ${
          working || shownNotice ? "" : captureReady ? "lit" : "arming"
        } ${refines && !shownNotice ? "has-refine" : ""} ${noticeClass}`}
        style={shownNotice ? { width: noticeWidth } : undefined}
        onClick={dismissNotice}
      >
        {shownNotice
          ? noticeRow
          : working
            ? workingRow(workLabel, true)
            : listeningRow(false, true)}
      </div>
    </div>
  );
};

export default RecordingOverlay;
