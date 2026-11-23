//! Fork addition: while a live stream records, paste each newly committed
//! part of the text as it arrives instead of everything once recording
//! stops; at stop only the rest is pasted. On with remote recognition
//! (`remote.incremental_paste`, or `"incremental_paste": true` in a
//! hand-written `remote_worker.json`).
//!
//! It stays off when the text pasted at stop can differ from what was
//! streamed: post-processing or a transcription hook is in use. A cancelled
//! dictation keeps whatever was already pasted.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use log::{debug, info, warn};
use tauri::AppHandle;

struct Session {
    /// Committed text already handed to the paste thread.
    queued: String,
    tx: Option<mpsc::Sender<String>>,
    worker: Option<JoinHandle<String>>,
    cancelled: Arc<AtomicBool>,
}

pub struct Finished {
    pub remaining: String,
    pub needs_review: bool,
}

fn paste_queue(
    rx: mpsc::Receiver<String>,
    cancelled: &AtomicBool,
    mut paste: impl FnMut(&str) -> Result<crate::clipboard::PasteOutcome, String>,
) -> String {
    let mut pasted = String::new();
    for fragment in rx {
        if cancelled.load(Ordering::Acquire) {
            break;
        }
        match paste(&fragment) {
            Ok(crate::clipboard::PasteOutcome::Pasted) => pasted.push_str(&fragment),
            Ok(crate::clipboard::PasteOutcome::Blocked) => {
                warn!("Incremental paste blocked; retaining the rest for final paste");
                break;
            }
            Err(e) => {
                warn!("Incremental paste failed; retaining the rest for final paste: {e}");
                break;
            }
        }
    }
    pasted
}

static SESSION: Mutex<Option<Session>> = Mutex::new(None);

fn lock() -> std::sync::MutexGuard<'static, Option<Session>> {
    SESSION
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn config_enabled(app: &AppHandle) -> bool {
    let Ok(dir) = crate::portable::app_data_dir(app) else {
        return false;
    };
    if dir.join("hooks").join("transcription").is_file() {
        return false;
    }
    crate::remote::incremental_paste()
}

/// Arms a session for the dictation about to start (or disarms it).
pub fn begin(app: &AppHandle, post_process: bool) {
    abort();
    // Text the LLM refines is pasted once, after the rewrite (`refine::wants`).
    if post_process || !config_enabled(app) {
        return;
    }
    let (tx, rx) = mpsc::channel::<String>();
    let paste_app = app.clone();
    let cancelled = Arc::new(AtomicBool::new(false));
    let worker_cancelled = Arc::clone(&cancelled);
    let worker = std::thread::Builder::new()
        .name("incremental-paste".into())
        .spawn(move || {
            paste_queue(rx, &worker_cancelled, |fragment| {
                crate::clipboard::paste_fragment(fragment.to_owned(), paste_app.clone())
            })
        });
    match worker {
        Ok(worker) => {
            debug!("Incremental paste armed");
            *lock() = Some(Session {
                queued: String::new(),
                tx: Some(tx),
                worker: Some(worker),
                cancelled,
            });
        }
        Err(e) => warn!("Incremental paste unavailable: {e}"),
    }
}

/// The stream's committed text (append-only); pastes what is new.
pub fn committed(committed: &str) {
    let mut guard = lock();
    let Some(session) = guard.as_mut() else {
        return;
    };
    let Some(tx) = session.tx.as_ref() else {
        return;
    };
    let Some(new) = committed.strip_prefix(session.queued.as_str()) else {
        // Committed text is append-only; anything else means this session
        // can no longer tell what was pasted, so leave the rest to the stop.
        warn!("Stream committed text was rewritten; incremental paste stops here");
        session.tx = None;
        return;
    };
    if new.is_empty() {
        return;
    }
    if tx.send(new.to_string()).is_ok() {
        session.queued = committed.to_string();
    } else {
        warn!("Incremental paste worker stopped; retaining the rest for final paste");
        session.tx = None;
    }
}

/// Ends the session and returns the part of `final_text` still to paste.
/// Without a session that is all of it.
pub fn finish(final_text: String) -> Finished {
    let Some(mut session) = lock().take() else {
        return remainder(final_text, "");
    };
    drop(session.tx.take());
    let pasted = match session.worker.take().map(JoinHandle::join) {
        Some(Ok(pasted)) => pasted,
        _ => {
            warn!("Incremental paste worker failed; final text may repeat already pasted text");
            return Finished {
                remaining: final_text,
                needs_review: true,
            };
        }
    };
    remainder(final_text, &pasted)
}

fn remainder(final_text: String, pasted: &str) -> Finished {
    if let Some(rest) = final_text.strip_prefix(pasted) {
        info!(
            "Incremental paste: {} chars pasted while recording, {} at stop",
            pasted.chars().count(),
            rest.chars().count()
        );
        return Finished {
            remaining: rest.to_owned(),
            needs_review: false,
        };
    }
    // The final text differs from the streamed one (for example a batch
    // retry): paste what follows their common prefix.
    let common: usize = final_text
        .chars()
        .zip(pasted.chars())
        .take_while(|(a, b)| a == b)
        .map(|(a, _)| a.len_utf8())
        .sum();
    warn!(
        "Final text diverges from the pasted stream after {} bytes; appending the corrected suffix, review the saved transcript",
        common
    );
    Finished {
        remaining: final_text[common..].to_owned(),
        needs_review: true,
    }
}

/// Stops pasting for the current dictation (cancel); already pasted text stays.
pub fn abort() {
    if let Some(mut session) = lock().take() {
        session.cancelled.store(true, Ordering::Release);
        drop(session.tx.take());
        // An in-flight paste may finish, but discard queued fragments on cancel.
        drop(session.worker.take());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clipboard::PasteOutcome;

    fn queue(parts: &[&str]) -> mpsc::Receiver<String> {
        let (tx, rx) = mpsc::channel();
        for part in parts {
            tx.send((*part).to_owned()).unwrap();
        }
        rx
    }

    #[test]
    fn failed_fragments_are_retried_at_finish_without_skipping_later_words() {
        for failure in [Ok(PasteOutcome::Blocked), Err("paste error".to_owned())] {
            let mut attempts = 0;
            let pasted = paste_queue(
                queue(&["first ", "second ", "third"]),
                &AtomicBool::new(false),
                |_| {
                    attempts += 1;
                    if attempts == 1 {
                        Ok(PasteOutcome::Pasted)
                    } else {
                        failure.clone()
                    }
                },
            );
            assert_eq!(attempts, 2);
            assert_eq!(pasted, "first ");
            let result = remainder("first second third".to_owned(), &pasted);
            assert_eq!(result.remaining, "second third");
            assert!(!result.needs_review);
        }
    }

    #[test]
    fn cancellation_discards_queued_fragments() {
        let cancelled = AtomicBool::new(false);
        let pasted = paste_queue(queue(&["first", "second"]), &cancelled, |_| {
            cancelled.store(true, Ordering::Release);
            Ok(PasteOutcome::Pasted)
        });
        assert_eq!(pasted, "first");
    }

    #[test]
    fn final_rewrites_preserve_the_entire_corrected_unicode_suffix() {
        let result = remainder("你好新文字".to_owned(), "你好旧内容很长");
        assert_eq!(result.remaining, "新文字");
        assert!(result.needs_review);
        let result = remainder("replacement".to_owned(), "unrelated text");
        assert_eq!(result.remaining, "replacement");
        assert!(result.needs_review);
    }
}
