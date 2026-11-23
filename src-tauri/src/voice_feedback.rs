//! Fork: the voice-feedback tool (tools/feedback/voice_feedback.py) shows in Handy's own tray icon
//! instead of a second one: an orange dot on the idle icon while suggestions or notices wait for review,
//! a blue dot while a report is being analysed, and menu entries to review, report the last dictation or
//! read the guide. State comes from %LOCALAPPDATA%\handy-remote\pending.json (older installs:
//! %LOCALAPPDATA%\voice-asr), which the script rewrites (including its own path in "script"); nothing
//! ever pops up by itself.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, SystemTime};
use tauri::AppHandle;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct VoiceFeedback {
    /// Suggestions + notices waiting for review.
    pub pending: u32,
    /// A report is being analysed.
    pub analyzing: bool,
    /// voice_feedback.py, when the feedback loop is installed.
    pub script: Option<String>,
}

static STATE: Mutex<VoiceFeedback> = Mutex::new(VoiceFeedback {
    pending: 0,
    analyzing: false,
    script: None,
});

fn pending_path() -> Option<PathBuf> {
    let base = PathBuf::from(std::env::var_os("LOCALAPPDATA")?);
    let current = base.join("handy-remote").join("pending.json");
    let legacy = base.join("voice-asr").join("pending.json");
    Some(if !current.is_file() && legacy.is_file() {
        legacy
    } else {
        current
    })
}

fn parse(text: &str) -> VoiceFeedback {
    let v: serde_json::Value =
        serde_json::from_str(text.trim_start_matches('\u{feff}')).unwrap_or_default();
    VoiceFeedback {
        pending: v.get("count").and_then(|c| c.as_u64()).unwrap_or(0) as u32,
        analyzing: v
            .get("analyzing")
            .and_then(|a| a.as_bool())
            .unwrap_or(false),
        script: v
            .get("script")
            .and_then(|s| s.as_str())
            .filter(|s| std::path::Path::new(s).is_file())
            .map(str::to_owned),
    }
}

pub fn current() -> VoiceFeedback {
    STATE.lock().map(|s| s.clone()).unwrap_or_default()
}

/// Polls pending.json (mtime only, every 2 s) and refreshes the tray when its state changes.
pub fn start_watch(app: &AppHandle) {
    let app = app.clone();
    let _ = std::thread::Builder::new()
        .name("voice-feedback".into())
        .spawn(move || {
            let mut last: Option<Option<SystemTime>> = None;
            loop {
                let path = pending_path();
                let mtime = path
                    .as_ref()
                    .and_then(|p| std::fs::metadata(p).ok())
                    .and_then(|m| m.modified().ok());
                if last != Some(mtime) {
                    last = Some(mtime);
                    let next = path
                        .and_then(|p| std::fs::read_to_string(p).ok())
                        .map(|t| parse(&t))
                        .unwrap_or_default();
                    let changed = STATE
                        .lock()
                        .map(|mut s| std::mem::replace(&mut *s, next.clone()) != next)
                        .unwrap_or(false);
                    if changed {
                        log::info!(
                            "voice feedback: {} pending, analyzing={}",
                            next.pending,
                            next.analyzing
                        );
                        crate::tray::refresh_tray_icon(&app);
                    }
                }
                std::thread::sleep(Duration::from_secs(2));
            }
        });
}

/// Actions another process may request with `handy --voice-feedback <action>`.
pub const ACTIONS: [&str; 3] = ["report", "review", "guide"];

/// The action of a `--voice-feedback` request forwarded to the single instance. Accepts the
/// separate-argument form plus `--voice-feedback=report` and `"--voice-feedback report"` (one
/// argument), which hotkey tools produce when they join arguments themselves.
pub fn action_from_args(args: &[String]) -> Option<String> {
    args.iter().enumerate().find_map(|(i, arg)| {
        let rest = arg.strip_prefix("--voice-feedback")?;
        match rest.chars().next() {
            None => Some(args.get(i + 1).cloned().unwrap_or_default()),
            Some('=' | ' ') => Some(rest[1..].trim().to_string()),
            _ => None,
        }
    })
}

/// `--voice-feedback <action>` from a hotkey: inside Remote Desktop (with rdp_dictation on) the
/// remote session's Handy did the last dictation, so it opens the feedback there.
pub fn open(action: &str) {
    if !ACTIONS.contains(&action) {
        log::warn!("voice feedback: unknown action {action:?}");
        return;
    }
    #[cfg(target_os = "windows")]
    if crate::caret::forward_feedback_to_remote_desktop(action) {
        return;
    }
    run(action);
}

/// Runs `voice_feedback.py <action>` (review / report / guide) with the windowed Python launcher.
pub fn run(action: &str) {
    let Some(script) = current().script else {
        log::warn!("voice feedback: script not found (pending.json has no valid \"script\")");
        return;
    };
    match std::process::Command::new("pyw.exe")
        .args(["-3", &script, action])
        .spawn()
    {
        Ok(_) => log::info!("voice feedback: started {action}"),
        Err(e) => log::warn!("voice feedback: failed to start {action}: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_counts_and_ignores_missing_script() {
        let s = parse("\u{feff}{\"count\": 2, \"analyzing\": true, \"script\": \"C:/nope/x.py\"}");
        assert_eq!(s.pending, 2);
        assert!(s.analyzing);
        assert_eq!(s.script, None);
        assert_eq!(parse("not json"), VoiceFeedback::default());
    }

    #[test]
    fn reads_the_action_in_every_argument_form() {
        let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let action = |v: &[&str]| action_from_args(&args(v));
        assert_eq!(
            action(&["handy.exe", "--voice-feedback", "report"]).as_deref(),
            Some("report")
        );
        assert_eq!(
            action(&["handy.exe", "--voice-feedback report"]).as_deref(),
            Some("report")
        );
        assert_eq!(
            action(&["handy.exe", "--voice-feedback=review"]).as_deref(),
            Some("review")
        );
        assert_eq!(action(&["handy.exe", "--voice-feedbackx"]), None);
        assert_eq!(action(&["handy.exe", "--toggle-transcription"]), None);
    }
}
