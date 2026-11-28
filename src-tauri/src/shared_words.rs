//! Fork: the custom words (Settings → custom words) are shared through the Handy Remote server's
//! lexicon, so every PC that uses the server sees and uses the same list.
//!
//! A sync is a three-way merge of this PC's list, the list as of the last sync
//! (`custom_words_synced`) and the server's `hotwords`: words added here are added on the server,
//! words removed here are removed there, and everything else follows the server (words added by
//! another PC or the feedback tool, or removed there). The server write is a compare-and-swap
//! (`LexiconPut` with the hash that was read), so concurrent edits are never lost. Afterwards both
//! lists equal the merged result. Syncs run at startup, every few minutes, after each edit and
//! when the settings page opens; without a server, or with an older one, nothing changes.

use std::collections::HashSet;
use std::sync::{mpsc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use log::{info, warn};
use serde::Serialize;
use serde_json::{json, Value};
use specta::Type;
use tauri::{AppHandle, Emitter};

use crate::remote::{server_spec, ServerSession};
use crate::settings::{get_settings, write_settings};

const FIRST_DELAY: Duration = Duration::from_secs(10);
const INTERVAL: Duration = Duration::from_secs(300);
const CALL_TIMEOUT: Duration = Duration::from_secs(20);
const ATTEMPTS: usize = 3;

#[derive(Serialize, Clone, Debug, Type, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum SharedWordsState {
    /// Recognition does not use a Handy Remote server: the list stays on this PC.
    Off,
    /// No sync finished yet.
    Pending,
    Synced,
    /// The server is too old to share the list.
    Unsupported,
    Failed,
}

#[derive(Serialize, Clone, Debug, Type)]
pub struct SharedWordsStatus {
    pub state: SharedWordsState,
    pub words: u32,
    /// Unix time (ms) of the last successful sync.
    pub synced_at: Option<u64>,
    pub error: Option<String>,
}

static STATUS: Mutex<SharedWordsStatus> = Mutex::new(SharedWordsStatus {
    state: SharedWordsState::Pending,
    words: 0,
    synced_at: None,
    error: None,
});
static SYNC: Mutex<()> = Mutex::new(());
static TRIGGER: OnceLock<Mutex<mpsc::Sender<()>>> = OnceLock::new();

enum SyncError {
    Unsupported,
    Failed(String),
}

fn key(word: &str) -> String {
    word.trim().to_lowercase()
}

/// The merged list: the server's words minus those removed here since the last sync, plus those
/// added here since then. Case-insensitive; keeps the server's order and spelling.
pub fn merge(local: &[String], base: &[String], server: &[String]) -> Vec<String> {
    let local_keys: HashSet<String> = local.iter().map(|w| key(w)).collect();
    let base_keys: HashSet<String> = base.iter().map(|w| key(w)).collect();
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for w in server {
        let k = key(w);
        let removed_here = base_keys.contains(&k) && !local_keys.contains(&k);
        if !k.is_empty() && !removed_here && seen.insert(k) {
            out.push(w.trim().to_string());
        }
    }
    for w in local {
        let k = key(w);
        if !k.is_empty() && !base_keys.contains(&k) && seen.insert(k) {
            out.push(w.trim().to_string());
        }
    }
    out
}

fn reply_error(reply: &Value) -> SyncError {
    if let Some(e) = reply.get("BackendError").and_then(Value::as_str) {
        return SyncError::Failed(e.to_string());
    }
    // An older server answers requests it does not know with a plain error.
    SyncError::Unsupported
}

/// One sync. Ok(None): there is no server to sync with.
fn sync_once(app: &AppHandle) -> Result<Option<usize>, SyncError> {
    let Some(spec) = server_spec() else {
        return Ok(None);
    };
    let mut session = ServerSession::open(&spec).map_err(SyncError::Failed)?;
    for _ in 0..ATTEMPTS {
        let settings = get_settings(app);
        let reply = session
            .call(&json!({"LexiconGet": {}}), CALL_TIMEOUT)
            .map_err(SyncError::Failed)?;
        let Some(lexicon) = reply.get("Lexicon") else {
            return Err(reply_error(&reply));
        };
        let sha = lexicon.get("sha256").and_then(Value::as_str).unwrap_or("");
        let mut data = lexicon.get("data").cloned().unwrap_or_else(|| json!({}));
        if !data.is_object() {
            data = json!({});
        }
        let server: Vec<String> = data
            .get("hotwords")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|w| w.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        let merged = merge(
            &settings.custom_words,
            &settings.custom_words_synced,
            &server,
        );
        if merged != server {
            data["hotwords"] = json!(merged);
            let reply = session
                .call(
                    &json!({"LexiconPut": {"data": data, "base_sha256": sha}}),
                    CALL_TIMEOUT,
                )
                .map_err(SyncError::Failed)?;
            if reply.get("Lexicon").is_none() {
                match reply_error(&reply) {
                    // Changed on the server meanwhile: read it again.
                    SyncError::Failed(e) if e.contains("changed on the server") => continue,
                    e => return Err(e),
                }
            }
            info!(
                "shared custom words: server list updated ({} -> {} words)",
                server.len(),
                merged.len()
            );
        }
        let mut now = get_settings(app);
        if now.custom_words != settings.custom_words {
            // Edited on this PC meanwhile: merge again.
            continue;
        }
        let changed = now.custom_words != merged;
        if changed || now.custom_words_synced != merged {
            now.custom_words = merged.clone();
            now.custom_words_synced = merged.clone();
            write_settings(app, now);
        }
        if changed {
            let _ = app.emit(
                "settings-changed",
                json!({"setting": "custom_words", "value": merged}),
            );
        }
        return Ok(Some(merged.len()));
    }
    Err(SyncError::Failed(
        "the word list kept changing during the sync; it is retried shortly".into(),
    ))
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Sync now (one at a time) and publish the outcome.
pub fn sync(app: &AppHandle) -> SharedWordsStatus {
    let _guard = SYNC.lock().unwrap_or_else(|e| e.into_inner());
    let previous = status();
    let next = match sync_once(app) {
        Ok(None) => SharedWordsStatus {
            state: SharedWordsState::Off,
            words: get_settings(app).custom_words.len() as u32,
            synced_at: None,
            error: None,
        },
        Ok(Some(n)) => SharedWordsStatus {
            state: SharedWordsState::Synced,
            words: n as u32,
            synced_at: Some(now_ms()),
            error: None,
        },
        Err(SyncError::Unsupported) => SharedWordsStatus {
            state: SharedWordsState::Unsupported,
            words: get_settings(app).custom_words.len() as u32,
            synced_at: None,
            error: None,
        },
        Err(SyncError::Failed(e)) => SharedWordsStatus {
            state: SharedWordsState::Failed,
            words: get_settings(app).custom_words.len() as u32,
            synced_at: previous.synced_at,
            error: Some(e),
        },
    };
    if next.state != previous.state || next.error != previous.error {
        match &next.error {
            Some(e) => warn!("shared custom words: sync failed: {e}"),
            None => info!(
                "shared custom words: {:?}, {} words",
                next.state, next.words
            ),
        }
    }
    if let Ok(mut s) = STATUS.lock() {
        *s = next.clone();
    }
    let _ = app.emit("shared-words-status", next.clone());
    next
}

pub fn status() -> SharedWordsStatus {
    STATUS
        .lock()
        .map(|s| s.clone())
        .unwrap_or_else(|e| e.into_inner().clone())
}

/// Ask the background thread for a sync soon (after an edit).
pub fn request_sync() {
    if let Some(tx) = TRIGGER.get() {
        if let Ok(tx) = tx.lock() {
            let _ = tx.send(());
        }
    }
}

/// Start the background syncs.
pub fn start(app: &AppHandle) {
    let (tx, rx) = mpsc::channel::<()>();
    if TRIGGER.set(Mutex::new(tx)).is_err() {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        let mut wait = FIRST_DELAY;
        loop {
            match rx.recv_timeout(wait) {
                Err(mpsc::RecvTimeoutError::Disconnected) => return,
                _ => while rx.try_recv().is_ok() {},
            }
            sync(&app);
            wait = INTERVAL;
        }
    });
}

#[tauri::command]
#[specta::specta]
pub fn shared_words_status() -> SharedWordsStatus {
    status()
}

/// Sync now and return the outcome (the settings page calls this when it opens).
#[tauri::command]
#[specta::specta]
pub async fn sync_shared_words(app: AppHandle) -> Result<SharedWordsStatus, String> {
    tauri::async_runtime::spawn_blocking(move || sync(&app))
        .await
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::merge;

    fn v(words: &[&str]) -> Vec<String> {
        words.iter().map(|w| w.to_string()).collect()
    }

    #[test]
    fn first_sync_unites_both_lists() {
        let out = merge(&v(&["Paseo", "docker"]), &[], &v(&["Docker", "UEFI"]));
        assert_eq!(out, v(&["Docker", "UEFI", "Paseo"]));
    }

    #[test]
    fn edits_here_and_on_the_server_both_survive() {
        let base = v(&["A", "B", "C"]);
        // Here: removed B, added D. Server (another PC): removed C, added E.
        let out = merge(&v(&["A", "C", "D"]), &base, &v(&["A", "B", "E"]));
        assert_eq!(out, v(&["A", "E", "D"]));
    }

    #[test]
    fn removal_here_drops_the_word_whatever_its_case() {
        let out = merge(&v(&["A"]), &v(&["A", "vs code"]), &v(&["A", "VS Code"]));
        assert_eq!(out, v(&["A"]));
    }

    #[test]
    fn blanks_and_duplicates_are_dropped() {
        let out = merge(&v(&[" ", "x "]), &[], &v(&["X", "x", ""]));
        assert_eq!(out, v(&["X"]));
    }
}
