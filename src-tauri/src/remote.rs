//! Fork: speech recognition on a Handy Remote server.
//!
//! The transcription worker normally runs as a child process of this app. With remote recognition
//! enabled it is replaced by a command that carries the same framed worker protocol to the server:
//! `ssh -W 127.0.0.1:<port> user@host` (the server binds loopback only, so SSH does authentication
//! and encryption), or this executable in relay mode (`--tcp-relay 127.0.0.1:<port>`) when the
//! server runs on this machine. The settings live in Handy's settings store (`remote`); a
//! hand-written `<app data>/remote_worker.json` (`{command, args, incremental_paste}`) is still
//! honoured while the settings page has not been used, for older installs and custom commands.
//!
//! The RDP caret lookup (`caret.rs`) reuses the same connection settings with the caret helper port,
//! and `remote_connection.json` tells the feedback tool how to reach the server.

use std::io::{BufRead, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{mpsc, RwLock};
use std::time::{Duration, Instant};

use log::{info, warn};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use specta::Type;
use tauri::{AppHandle, Emitter, Manager};

use crate::settings::{get_settings, write_settings};

/// First argument that turns the executable into a stdin/stdout <-> TCP relay.
pub const RELAY_FLAG: &str = "--tcp-relay";
/// Id (file stem) of the placeholder model that stands for the remote server in the model list.
pub const REMOTE_MODEL_ID: &str = "handy-remote-server";
const IDENTIFIER: &str = "com.musingzone.handy-remote";
const LEGACY_IDENTIFIER: &str = "com.pais.handy";
const LEGACY_FILE: &str = "remote_worker.json";
const KEY_FILE: &str = "remote_ed25519";
const KNOWN_HOSTS_FILE: &str = "remote_known_hosts";
const CONNECTION_FILE: &str = "remote_connection.json";
const PAIRING_PREFIX: &str = "handy-remote-pair:";
pub(crate) const STUB_LANGUAGES: [&str; 15] = [
    "zh", "en", "yue", "ja", "ko", "de", "fr", "es", "ru", "it", "pt", "ar", "id", "th", "vi",
];

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq, Type)]
#[serde(default)]
pub struct RemoteSettings {
    pub enabled: bool,
    /// Server host name or address; empty, `localhost` or `127.0.0.1` means this machine (no SSH).
    pub host: String,
    pub user: String,
    pub ssh_port: u16,
    /// Private key for SSH; empty uses `<app data>/remote_ed25519` if it exists, else ssh's defaults.
    pub identity_file: String,
    /// ssh executable; empty means auto-detect.
    pub ssh_path: String,
    pub server_port: u16,
    /// The server's SSH host key (`ssh-ed25519 AAAA…`) from the pairing code; pinned when set.
    pub host_key: String,
    pub label: String,
    /// Paste each finished sentence while still recording.
    pub incremental_paste: bool,
    /// Follow the caret of the remote session inside a Remote Desktop window (Windows).
    pub rdp_caret: bool,
    pub caret_port: u16,
    /// With a Remote Desktop window in front, hand the dictation toggle to Handy inside the remote
    /// session (which records the redirected microphone) through the caret helper (Windows).
    pub rdp_dictation: bool,
}

impl Default for RemoteSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            host: String::new(),
            user: String::new(),
            ssh_port: 22,
            identity_file: String::new(),
            ssh_path: String::new(),
            server_port: 47821,
            host_key: String::new(),
            label: String::new(),
            incremental_paste: true,
            rdp_caret: true,
            caret_port: 47823,
            rdp_dictation: false,
        }
    }
}

/// A command that speaks the worker protocol on stdin/stdout.
#[derive(Clone, PartialEq, Eq)]
pub struct Spec {
    pub program: PathBuf,
    pub args: Vec<String>,
    /// Extra environment; values may be secret (cloud API keys) and are never logged.
    pub env: Vec<(String, String)>,
}

impl std::fmt::Debug for Spec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Spec")
            .field("program", &self.program)
            .field("args", &self.args)
            .field("env", &self.env.iter().map(|(k, _)| k).collect::<Vec<_>>())
            .finish()
    }
}

impl Spec {
    pub fn command(&self) -> Command {
        let mut c = Command::new(&self.program);
        c.args(&self.args);
        c.envs(self.env.iter().map(|(k, v)| (k, v)));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        c
    }
}

#[derive(Deserialize, Clone, Debug)]
struct Legacy {
    #[serde(default = "yes")]
    enabled: bool,
    command: String,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    incremental_paste: bool,
    #[serde(default)]
    rdp_dictation: bool,
}

fn yes() -> bool {
    true
}

#[derive(Clone, Debug)]
struct State {
    data_dir: PathBuf,
    settings: RemoteSettings,
    legacy: Option<Legacy>,
}

static STATE: RwLock<Option<State>> = RwLock::new(None);

fn is_local(host: &str) -> bool {
    matches!(
        host.trim().to_ascii_lowercase().as_str(),
        "" | "localhost" | "127.0.0.1" | "::1"
    )
}

/// ssh executable: Windows OpenSSH, then Git's ssh, then PATH.
pub fn detect_ssh() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    #[cfg(windows)]
    {
        if let Some(root) = std::env::var_os("SystemRoot") {
            candidates.push(PathBuf::from(root).join(r"System32\OpenSSH\ssh.exe"));
        }
        for var in ["ProgramFiles", "ProgramW6432"] {
            if let Some(pf) = std::env::var_os(var) {
                candidates.push(PathBuf::from(pf).join(r"Git\usr\bin\ssh.exe"));
            }
        }
    }
    let exe = if cfg!(windows) { "ssh.exe" } else { "ssh" };
    if let Some(path) = std::env::var_os("PATH") {
        candidates.extend(std::env::split_paths(&path).map(|d| d.join(exe)));
    }
    candidates.into_iter().find(|p| p.is_file())
}

fn ssh_program(r: &RemoteSettings) -> Option<PathBuf> {
    let configured = r.ssh_path.trim();
    if configured.is_empty() {
        detect_ssh()
    } else {
        Some(PathBuf::from(configured))
    }
}

fn identity(data_dir: &Path, r: &RemoteSettings) -> Option<PathBuf> {
    let configured = r.identity_file.trim();
    if !configured.is_empty() {
        return Some(PathBuf::from(configured));
    }
    let own = data_dir.join(KEY_FILE);
    own.is_file().then_some(own)
}

fn target(r: &RemoteSettings) -> String {
    if r.user.trim().is_empty() {
        r.host.trim().to_string()
    } else {
        format!("{}@{}", r.user.trim(), r.host.trim())
    }
}

/// `type base64` of a host key line, without the comment.
fn host_key_core(key: &str) -> Option<String> {
    let mut parts = key.split_whitespace();
    let kind = parts.next()?;
    let data = parts.next()?;
    Some(format!("{kind} {data}"))
}

fn known_hosts_line(r: &RemoteSettings) -> Option<String> {
    let key = host_key_core(&r.host_key)?;
    let host = r.host.trim();
    Some(if r.ssh_port == 22 {
        format!("{host} {key}\n")
    } else {
        format!("[{host}]:{} {key}\n", r.ssh_port)
    })
}

/// ssh options shared by the worker, the caret tunnel and the connection test.
fn ssh_options(data_dir: &Path, r: &RemoteSettings) -> Vec<String> {
    let mut args: Vec<String> = Vec::new();
    if r.ssh_port != 22 {
        args.extend(["-p".into(), r.ssh_port.to_string()]);
    }
    if let Some(key) = identity(data_dir, r) {
        args.extend([
            "-i".into(),
            key.to_string_lossy().into_owned(),
            "-o".into(),
            "IdentitiesOnly=yes".into(),
        ]);
    }
    if known_hosts_line(r).is_some() {
        let file = data_dir
            .join(KNOWN_HOSTS_FILE)
            .to_string_lossy()
            .replace('\\', "/");
        args.extend([
            "-o".into(),
            format!("UserKnownHostsFile=\"{file}\""),
            "-o".into(),
            "StrictHostKeyChecking=yes".into(),
        ]);
    } else {
        args.extend(["-o".into(), "StrictHostKeyChecking=accept-new".into()]);
    }
    for opt in [
        "BatchMode=yes",
        "ConnectTimeout=8",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=2",
    ] {
        args.extend(["-o".into(), opt.into()]);
    }
    args
}

fn build(data_dir: &Path, r: &RemoteSettings, port: u16) -> Option<Spec> {
    if is_local(&r.host) {
        return Some(Spec {
            program: std::env::current_exe().ok()?,
            args: vec![RELAY_FLAG.into(), format!("127.0.0.1:{port}")],
            env: vec![],
        });
    }
    let mut args = ssh_options(data_dir, r);
    args.extend(["-W".into(), format!("127.0.0.1:{port}"), target(r)]);
    Some(Spec {
        program: ssh_program(r)?,
        args,
        env: vec![],
    })
}

fn read_state() -> Option<State> {
    STATE.read().ok().and_then(|s| s.clone())
}

/// The worker command, or None for the local engine.
pub fn worker_spec() -> Option<Spec> {
    if let Some(spec) = crate::cloud_asr::worker_spec() {
        return Some(spec);
    }
    let st = read_state()?;
    if st.settings.enabled {
        return build(&st.data_dir, &st.settings, st.settings.server_port);
    }
    let l = st.legacy?;
    Some(Spec {
        program: PathBuf::from(l.command),
        args: l.args,
        env: vec![],
    })
}

/// Whether recognition goes to a remote server or a cloud service.
pub fn active() -> bool {
    crate::cloud_asr::active()
        || read_state().is_some_and(|st| st.settings.enabled || st.legacy.is_some())
}

/// Paste finished sentences while recording.
pub fn incremental_paste() -> bool {
    if crate::cloud_asr::active() {
        return false;
    }
    match read_state() {
        Some(st) if st.settings.enabled => st.settings.incremental_paste,
        Some(st) => st.legacy.is_some_and(|l| l.incremental_paste),
        None => false,
    }
}

/// Hand dictation toggles to Handy inside a Remote Desktop session (needs the caret tunnel).
pub fn rdp_dictation() -> bool {
    match read_state() {
        Some(st) if st.settings.enabled => st.settings.rdp_dictation,
        Some(st) => st.legacy.is_some_and(|l| l.rdp_dictation),
        None => false,
    }
}

/// Tunnel command to the RDP caret helper: `HANDY_REMOTE_CARET` = `0` (off) or `host:port`
/// overrides the setting.
pub fn caret_spec() -> Option<Spec> {
    let st = read_state()?;
    let address = match std::env::var("HANDY_REMOTE_CARET") {
        Ok(v) if v == "0" => return None,
        Ok(v) if !v.trim().is_empty() => Some(v.trim().to_string()),
        _ => None,
    };
    if st.settings.enabled {
        if !st.settings.rdp_caret {
            return None;
        }
        let mut spec = build(&st.data_dir, &st.settings, st.settings.caret_port)?;
        if let Some(address) = address {
            let i = spec.args.len() - if is_local(&st.settings.host) { 1 } else { 2 };
            spec.args[i] = address;
        }
        return Some(spec);
    }
    let l = st.legacy?;
    let mut args = l.args;
    let w = args.iter().position(|a| a == "-W")?;
    *args.get_mut(w + 1)? = address.unwrap_or_else(|| "127.0.0.1:47823".into());
    Some(Spec {
        program: PathBuf::from(l.command),
        args,
        env: vec![],
    })
}

fn load_legacy(data_dir: &Path) -> Option<Legacy> {
    let path = data_dir.join(LEGACY_FILE);
    let text = std::fs::read_to_string(&path).ok()?;
    match serde_json::from_str::<Legacy>(&text) {
        Ok(l) if l.enabled => Some(l),
        Ok(_) => None,
        Err(e) => {
            warn!("ignoring {}: {e}", path.display());
            None
        }
    }
}

/// Connection details for the feedback tool (`tools/feedback`), which has no access to this store.
fn write_connection_file(st: &State) {
    let (enabled, ssh, ssh_args, target, port) = if st.settings.enabled {
        let r = &st.settings;
        if is_local(&r.host) {
            (true, None, vec![], None, r.server_port)
        } else {
            (
                true,
                ssh_program(r),
                ssh_options(&st.data_dir, r),
                Some(target(r)),
                r.server_port,
            )
        }
    } else if let Some(l) = &st.legacy {
        let mut args = l.args.clone();
        let target = args.pop();
        let mut port = 47821;
        if let Some(w) = args.iter().position(|a| a == "-W") {
            if let Some(p) = args
                .get(w + 1)
                .and_then(|a| a.rsplit(':').next())
                .and_then(|p| p.parse().ok())
            {
                port = p;
            }
            args.drain(w..(w + 2).min(args.len()));
        }
        (true, Some(PathBuf::from(&l.command)), args, target, port)
    } else {
        (false, None, vec![], None, st.settings.server_port)
    };
    let v = json!({
        "enabled": enabled,
        "ssh": ssh.map(|p| p.to_string_lossy().into_owned()),
        "ssh_args": ssh_args,
        "target": target,
        "server_port": port,
    });
    let path = st.data_dir.join(CONNECTION_FILE);
    let tmp = path.with_extension("json.tmp");
    let ok = std::fs::write(&tmp, serde_json::to_vec_pretty(&v).unwrap_or_default())
        .and_then(|_| std::fs::rename(&tmp, &path));
    if let Err(e) = ok {
        warn!("could not write {}: {e}", path.display());
    }
}

fn write_known_hosts(data_dir: &Path, r: &RemoteSettings) {
    if let Some(line) = known_hosts_line(r) {
        if let Err(e) = std::fs::write(data_dir.join(KNOWN_HOSTS_FILE), line) {
            warn!("could not write the pinned host key: {e}");
        }
    }
}

fn set_state(data_dir: PathBuf, settings: RemoteSettings) {
    let legacy = load_legacy(&data_dir);
    write_known_hosts(&data_dir, &settings);
    let st = State {
        data_dir,
        settings,
        legacy,
    };
    write_connection_file(&st);
    match build(&st.data_dir, &st.settings, st.settings.server_port) {
        Some(spec) if st.settings.enabled => info!(
            "remote recognition: {} {:?}",
            spec.program.display(),
            spec.args
        ),
        None if st.settings.enabled => warn!("remote recognition is on, but no ssh was found"),
        _ if st.legacy.is_some() => info!("remote recognition: {LEGACY_FILE}"),
        _ => {}
    }
    if let Ok(mut g) = STATE.write() {
        *g = Some(st);
    }
}

/// Load the remote settings at startup.
pub fn init(app: &AppHandle) {
    let Ok(data_dir) = crate::portable::app_data_dir(app) else {
        return;
    };
    set_state(data_dir, get_settings(app).remote);
}

// ---------------------------------------------------------------- relay mode

/// `--tcp-relay host:port` as the first argument: the address to relay to.
pub fn relay_invocation() -> Option<String> {
    let mut args = std::env::args();
    args.next();
    if args.next()? == RELAY_FLAG {
        args.next()
    } else {
        None
    }
}

/// Copy stdin to a TCP connection and the connection to stdout until either side closes.
pub fn run_relay(address: &str) -> i32 {
    let Ok(sock) = std::net::TcpStream::connect(address) else {
        eprintln!("cannot connect to {address}");
        return 1;
    };
    let _ = sock.set_nodelay(true);
    let Ok(mut up) = sock.try_clone() else {
        return 1;
    };
    std::thread::spawn(move || {
        let _ = std::io::copy(&mut std::io::stdin().lock(), &mut up);
        let _ = up.shutdown(std::net::Shutdown::Write);
    });
    let mut down = sock;
    let mut out = std::io::stdout().lock();
    let mut buf = [0u8; 64 * 1024];
    loop {
        match down.read(&mut buf) {
            Ok(0) | Err(_) => return 0,
            Ok(n) => {
                if out.write_all(&buf[..n]).and_then(|_| out.flush()).is_err() {
                    return 0;
                }
            }
        }
    }
}

// ---------------------------------------------------------------- legacy data

fn data_root() -> Option<PathBuf> {
    #[cfg(windows)]
    return std::env::var_os("APPDATA").map(PathBuf::from);
    #[cfg(target_os = "macos")]
    return std::env::var_os("HOME").map(|h| PathBuf::from(h).join("Library/Application Support"));
    #[cfg(all(unix, not(target_os = "macos")))]
    return std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")));
}

fn copy_tree(from: &Path, to: &Path, link_files: bool) -> std::io::Result<()> {
    std::fs::create_dir_all(to)?;
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let (src, dst) = (entry.path(), to.join(entry.file_name()));
        if entry.file_type()?.is_dir() {
            if entry.file_name() != "logs" {
                copy_tree(&src, &dst, link_files || entry.file_name() == "models")?;
            }
        } else if !dst.exists() {
            // Model files are large and never edited in place: a hard link shares them; anything
            // else is copied so the two apps never change each other's files.
            if !(link_files && std::fs::hard_link(&src, &dst).is_ok()) {
                std::fs::copy(&src, &dst)?;
            }
        }
    }
    Ok(())
}

/// First run of the renamed app: copy settings, history, recordings and models of an upstream
/// Handy install (never move: upstream Handy keeps working). Runs before Tauri starts.
pub fn migrate_legacy_data() {
    if crate::portable::is_portable() {
        return;
    }
    let Some(root) = data_root() else {
        return;
    };
    let (old, new) = (root.join(LEGACY_IDENTIFIER), root.join(IDENTIFIER));
    if new.join("settings_store.json").exists() || !old.join("settings_store.json").is_file() {
        return;
    }
    match copy_tree(&old, &new, false) {
        Ok(()) => {
            let _ = std::fs::write(
                new.join("migrated-from.txt"),
                format!("{}\n", old.display()),
            );
            eprintln!("[remote] copied {} to {}", old.display(), new.display());
        }
        Err(e) => eprintln!("[remote] copying {} failed: {e}", old.display()),
    }
}

// ---------------------------------------------------------------- placeholder model

fn gguf_str(s: &str) -> Vec<u8> {
    let mut v = (s.len() as u64).to_le_bytes().to_vec();
    v.extend_from_slice(s.as_bytes());
    v
}

/// GGUF header only: the model list needs a file with capabilities, the server ignores it.
fn stub_gguf(name: &str, streaming: bool) -> Vec<u8> {
    let mut langs = 8u32.to_le_bytes().to_vec();
    langs.extend((STUB_LANGUAGES.len() as u64).to_le_bytes());
    for l in STUB_LANGUAGES {
        langs.extend(gguf_str(l));
    }
    let kv: Vec<(&str, u32, Vec<u8>)> = vec![
        ("general.architecture", 8, gguf_str("qwen3_asr")),
        ("general.name", 8, gguf_str(name)),
        ("stt.capability.streaming", 7, vec![streaming as u8]),
        ("stt.capability.lang_detect", 7, vec![1]),
        ("stt.capability.translate", 7, vec![0]),
        ("general.languages", 9, langs),
    ];
    let mut out = Vec::new();
    out.extend(0x4655_4747u32.to_le_bytes());
    out.extend(3u32.to_le_bytes());
    out.extend(0u64.to_le_bytes());
    out.extend((kv.len() as u64).to_le_bytes());
    for (k, t, v) in kv {
        out.extend(gguf_str(k));
        out.extend(t.to_le_bytes());
        out.extend(v);
    }
    out
}

/// The placeholder model: the remote server (live preview) or, while it is on, the cloud service.
fn ensure_stub(app: &AppHandle, label: &str) -> Result<(), String> {
    let (name, streaming) = match crate::cloud_asr::model_label() {
        Some(name) => (name, false),
        None if label.is_empty() => ("Remote server".to_string(), true),
        None => (format!("Remote server: {label}"), true),
    };
    let dir = crate::portable::app_data_dir(app)
        .map_err(|e| e.to_string())?
        .join("models");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = dir.join(format!("{REMOTE_MODEL_ID}.gguf"));
    let bytes = stub_gguf(&name, streaming);
    if std::fs::read(&path).ok().as_deref() != Some(bytes.as_slice()) {
        std::fs::write(&path, bytes).map_err(|e| e.to_string())?;
    }
    let mm = app.state::<std::sync::Arc<crate::managers::model::ModelManager>>();
    mm.rescan_local_models().map_err(|e| e.to_string())
}

// ---------------------------------------------------------------- pairing, keys, test

fn base64url_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in s.trim().trim_end_matches('=').bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' | b'+' => 62,
            b'_' | b'/' => 63,
            _ => return None,
        } as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// Apply a `handy-remote-pair:…` code (printed by the server's `doctor.py pair`) to `base`.
pub fn apply_pairing(base: &RemoteSettings, code: &str) -> Result<RemoteSettings, String> {
    let body = code
        .trim()
        .strip_prefix(PAIRING_PREFIX)
        .ok_or("not a Handy Remote pairing code")?;
    let bytes = base64url_decode(body).ok_or("the pairing code is damaged")?;
    let v: Value = serde_json::from_slice(&bytes).map_err(|_| "the pairing code is damaged")?;
    let s = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_owned);
    let n = |k: &str| {
        v.get(k)
            .and_then(Value::as_u64)
            .and_then(|p| u16::try_from(p).ok())
    };
    let mut r = base.clone();
    r.host = s("host").ok_or("the pairing code has no host")?;
    r.user = s("user").unwrap_or_default();
    r.ssh_port = n("ssh_port").unwrap_or(22);
    r.server_port = n("server_port").unwrap_or(r.server_port);
    r.caret_port = n("caret_port").unwrap_or(r.caret_port);
    r.label = s("label").unwrap_or_default();
    r.host_key = s("host_key").unwrap_or_default();
    r.enabled = true;
    Ok(r)
}

#[derive(Serialize, Debug, Clone, Type)]
pub struct RemoteTestResult {
    pub connect_ms: u32,
    pub round_trip_ms: Option<u32>,
    pub label: Option<String>,
    pub description: Option<String>,
    pub loaded: Option<bool>,
    pub openvino: Option<String>,
    /// The server answered but does not know the Status request (older server).
    pub old_server: bool,
}

fn frame(msg: &Value) -> Vec<u8> {
    let j = serde_json::to_vec(msg).unwrap_or_default();
    let mut out = (j.len() as u32).to_le_bytes().to_vec();
    out.extend(j);
    out.extend(0u32.to_le_bytes());
    out
}

fn read_frame(r: &mut impl Read) -> std::io::Result<Value> {
    let mut n = [0u8; 4];
    r.read_exact(&mut n)?;
    let mut j = vec![0u8; u32::from_le_bytes(n) as usize];
    r.read_exact(&mut j)?;
    r.read_exact(&mut n)?;
    std::io::copy(
        &mut r.by_ref().take(u32::from_le_bytes(n) as u64),
        &mut std::io::sink(),
    )?;
    serde_json::from_slice(&j).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

/// Connect with `settings` (saved or not) and ask the server for its status.
pub fn test_connection(
    data_dir: &Path,
    settings: &RemoteSettings,
) -> Result<RemoteTestResult, String> {
    write_known_hosts(data_dir, settings);
    let spec = build(data_dir, settings, settings.server_port)
        .ok_or("no ssh executable was found; install the Windows OpenSSH client")?;
    let mut child = spec
        .command()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("{}: {e}", spec.program.display()))?;
    let (mut stdin, mut stdout, stderr) = (
        child.stdin.take().ok_or("no stdin")?,
        child.stdout.take().ok_or("no stdout")?,
        child.stderr.take().ok_or("no stderr")?,
    );
    let err_lines = std::thread::spawn(move || {
        std::io::BufReader::new(stderr)
            .lines()
            .map_while(Result::ok)
            .filter(|l| !l.trim().is_empty())
            .collect::<Vec<_>>()
    });
    let (tx, rx) = mpsc::channel();
    let t0 = Instant::now();
    std::thread::spawn(move || {
        for _ in 0..2 {
            let r = stdin
                .write_all(&frame(&json!({"Status": {}})))
                .and_then(|_| stdin.flush())
                .and_then(|_| read_frame(&mut stdout));
            if tx.send(r).is_err() {
                return;
            }
        }
    });
    let first = rx.recv_timeout(Duration::from_secs(20));
    let connect_ms = t0.elapsed().as_millis() as u32;
    let t1 = Instant::now();
    let second = rx.recv_timeout(Duration::from_secs(5));
    let round_trip_ms = matches!(second, Ok(Ok(_))).then(|| t1.elapsed().as_millis() as u32);
    let _ = child.kill();
    let _ = child.wait();
    let ssh_errors = err_lines.join().unwrap_or_default().join("\n");
    let reply = match first {
        Ok(Ok(v)) => v,
        Ok(Err(e)) if ssh_errors.is_empty() => return Err(format!("the connection closed: {e}")),
        Ok(Err(_)) => return Err(ssh_errors),
        Err(_) => return Err("no answer within 20 s".into()),
    };
    let status = reply.get("Status");
    let field = |k: &str| status.and_then(|s| s.get(k)).cloned();
    Ok(RemoteTestResult {
        connect_ms,
        round_trip_ms,
        label: field("label").and_then(|v| v.as_str().map(str::to_owned)),
        description: field("description").and_then(|v| v.as_str().map(str::to_owned)),
        loaded: field("loaded").and_then(|v| v.as_bool()),
        openvino: field("openvino").and_then(|v| v.as_str().map(str::to_owned)),
        old_server: status.is_none(),
    })
}

/// This app's SSH public key (`<app data>/remote_ed25519.pub`), created on request.
fn client_key(
    data_dir: &Path,
    settings: &RemoteSettings,
    create: bool,
) -> Result<Option<String>, String> {
    let key = data_dir.join(KEY_FILE);
    let public = data_dir.join(format!("{KEY_FILE}.pub"));
    if !public.is_file() && create {
        let ssh = ssh_program(settings).ok_or("no ssh executable was found")?;
        let keygen = ssh.with_file_name(if cfg!(windows) {
            "ssh-keygen.exe"
        } else {
            "ssh-keygen"
        });
        let host = std::env::var("COMPUTERNAME")
            .or_else(|_| std::env::var("HOSTNAME"))
            .unwrap_or_default();
        let mut c = Command::new(&keygen);
        c.args(["-q", "-t", "ed25519", "-N", "", "-C"])
            .arg(format!("handy-remote@{host}"))
            .arg("-f")
            .arg(&key)
            .stdin(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            c.creation_flags(0x0800_0000);
        }
        let out = c
            .output()
            .map_err(|e| format!("{}: {e}", keygen.display()))?;
        if !out.status.success() {
            return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
        }
    }
    match std::fs::read_to_string(&public) {
        Ok(text) => Ok(Some(text.trim().to_string())),
        Err(_) => Ok(None),
    }
}

// ---------------------------------------------------------------- commands

fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    crate::portable::app_data_dir(app).map_err(|e| e.to_string())
}

/// Save the remote settings and switch recognition over (or back to the local engine).
#[tauri::command]
#[specta::specta]
pub fn change_remote_settings(app: AppHandle, remote: RemoteSettings) -> Result<(), String> {
    let dir = data_dir(&app)?;
    let mut settings = get_settings(&app);
    settings.remote = remote.clone();
    let mut changed = remote.enabled;
    if remote.enabled && settings.cloud_asr.enabled {
        // One recognition service at a time.
        settings.cloud_asr.enabled = false;
        changed = true;
    }
    write_settings(&app, settings.clone());
    crate::cloud_asr::init(&app);
    if remote.enabled {
        // The settings page replaces a hand-written remote_worker.json from now on.
        let legacy = dir.join(LEGACY_FILE);
        if legacy.is_file() {
            let _ = std::fs::rename(&legacy, dir.join(format!("{LEGACY_FILE}.migrated")));
        }
    }
    switch_recognition(&app, remote, changed)
}

/// Apply new remote settings and point the model selection at the remote server or cloud service
/// (re-selecting reloads the worker), or back to no model when neither is on any more.
pub(crate) fn switch_recognition(
    app: &AppHandle,
    remote: RemoteSettings,
    reload: bool,
) -> Result<(), String> {
    let dir = data_dir(app)?;
    let was_remote = get_settings(app).selected_model == REMOTE_MODEL_ID;
    set_state(dir, remote.clone());
    let tm = app.state::<std::sync::Arc<crate::managers::transcription::TranscriptionManager>>();
    if remote.enabled || crate::cloud_asr::active() {
        ensure_stub(app, &remote.label)?;
        if reload || !was_remote {
            crate::commands::models::switch_active_model(app, REMOTE_MODEL_ID)?;
        }
    } else if was_remote {
        let _ = tm.unload_model();
        let mut settings = get_settings(app);
        settings.selected_model = String::new();
        write_settings(app, settings);
        let _ = app.emit(
            "model-state-changed",
            json!({"event_type": "selection_changed"}),
        );
    }
    crate::tray::update_tray_menu(app);
    Ok(())
}

#[tauri::command]
#[specta::specta]
pub fn remote_apply_pairing(
    remote: RemoteSettings,
    code: String,
) -> Result<RemoteSettings, String> {
    apply_pairing(&remote, &code)
}

#[tauri::command]
#[specta::specta]
pub async fn remote_test_connection(
    app: AppHandle,
    remote: RemoteSettings,
) -> Result<RemoteTestResult, String> {
    let dir = data_dir(&app)?;
    tauri::async_runtime::spawn_blocking(move || test_connection(&dir, &remote))
        .await
        .map_err(|e| e.to_string())?
}

/// The SSH public key to authorize on the server (`doctor.py authorize "<key>"`).
#[tauri::command]
#[specta::specta]
pub fn remote_client_key(
    app: AppHandle,
    remote: RemoteSettings,
    create: bool,
) -> Result<Option<String>, String> {
    client_key(&data_dir(&app)?, &remote, create)
}

#[tauri::command]
#[specta::specta]
pub fn remote_detect_ssh() -> Option<String> {
    detect_ssh().map(|p| p.to_string_lossy().into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn remote(host: &str) -> RemoteSettings {
        RemoteSettings {
            enabled: true,
            host: host.into(),
            user: "alice".into(),
            ssh_path: "ssh".into(),
            ..Default::default()
        }
    }

    #[test]
    fn ssh_command_forwards_the_server_port() {
        let dir = Path::new("data");
        let spec = build(dir, &remote("server.lan"), 47821).unwrap();
        let n = spec.args.len();
        assert_eq!(
            spec.args[n - 3..],
            ["-W", "127.0.0.1:47821", "alice@server.lan"]
        );
        assert!(spec
            .args
            .contains(&"StrictHostKeyChecking=accept-new".to_string()));
        assert!(!spec.args.contains(&"-p".to_string()));
    }

    #[test]
    fn pinned_host_key_and_port() {
        let mut r = remote("server.lan");
        r.ssh_port = 2222;
        r.host_key = "ssh-ed25519 AAAAC3Nza host@x".into();
        assert_eq!(
            known_hosts_line(&r).unwrap(),
            "[server.lan]:2222 ssh-ed25519 AAAAC3Nza\n"
        );
        let args = ssh_options(Path::new("d"), &r);
        assert!(args.contains(&"StrictHostKeyChecking=yes".to_string()));
        assert_eq!(args[..2], ["-p", "2222"]);
    }

    #[test]
    fn local_server_uses_the_relay() {
        let spec = build(Path::new("d"), &remote(""), 47821).unwrap();
        assert_eq!(spec.args, [RELAY_FLAG, "127.0.0.1:47821"]);
    }

    #[test]
    fn pairing_code_round_trip() {
        // {"v":1,"host":"srv","user":"bob","ssh_port":22,"server_port":47821,"caret_port":47823,
        //  "label":"Office PC","host_key":"ssh-ed25519 AAAA"}
        let json = r#"{"v":1,"host":"srv","user":"bob","ssh_port":22,"server_port":47821,"caret_port":47823,"label":"Office PC","host_key":"ssh-ed25519 AAAA"}"#;
        let code = format!("{PAIRING_PREFIX}{}", b64(json.as_bytes()));
        let r = apply_pairing(&RemoteSettings::default(), &code).unwrap();
        assert!(r.enabled);
        assert_eq!((r.host.as_str(), r.user.as_str()), ("srv", "bob"));
        assert_eq!(r.label, "Office PC");
        assert!(apply_pairing(&r, "hello").is_err());
    }

    fn b64(bytes: &[u8]) -> String {
        const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut out = String::new();
        for chunk in bytes.chunks(3) {
            let n = chunk.iter().fold(0u32, |a, &b| (a << 8) | b as u32) << (8 * (3 - chunk.len()));
            for i in 0..=chunk.len() {
                out.push(T[((n >> (18 - 6 * i)) & 63) as usize] as char);
            }
        }
        out
    }

    #[test]
    fn stub_model_is_a_gguf_header() {
        let b = stub_gguf("Remote server: Office PC", true);
        assert_eq!(&b[..4], b"GGUF");
        assert!(String::from_utf8_lossy(&b).contains("Remote server: Office PC"));
    }

    #[test]
    fn copy_tree_skips_logs_and_keeps_existing_files() {
        let tmp = tempfile::tempdir().unwrap();
        let (old, new) = (tmp.path().join("old"), tmp.path().join("new"));
        std::fs::create_dir_all(old.join("logs")).unwrap();
        std::fs::create_dir_all(old.join("models")).unwrap();
        std::fs::write(old.join("settings_store.json"), "{}").unwrap();
        std::fs::write(old.join("logs/a.log"), "x").unwrap();
        std::fs::write(old.join("models/m.gguf"), "model").unwrap();
        std::fs::create_dir_all(&new).unwrap();
        std::fs::write(new.join("settings_store.json"), "mine").unwrap();
        copy_tree(&old, &new, false).unwrap();
        assert_eq!(
            std::fs::read_to_string(new.join("settings_store.json")).unwrap(),
            "mine"
        );
        assert_eq!(
            std::fs::read_to_string(new.join("models/m.gguf")).unwrap(),
            "model"
        );
        assert!(!new.join("logs").exists());
    }
}
