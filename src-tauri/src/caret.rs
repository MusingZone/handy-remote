//! Fork: text caret of the foreground app (Windows), so the overlay can sit just under it like an
//! IME candidate window. Tries, in order: the Win32 system caret (GetGUIThreadInfo: classic Win32
//! controls, Office), the MSAA caret object (OBJID_CARET: Chromium/Electron/WebView2 apps such as
//! VS Code, Edge, Teams), and the UI Automation caret range (TextPattern2/TextPattern: WinUI, Windows
//! Terminal). Every lookup runs on a short-lived MTA thread with a timeout, so a hung target app can
//! never stall the UI thread. Set HANDY_OVERLAY_CARET=0 to keep the overlay at the screen edge.
//!
//! Inside a Remote Desktop window (mstsc) the remote app's caret is invisible here, so it is asked
//! from the caret helper (tools/caret-helper) in the remote session over a persistent tunnel built
//! from the remote recognition settings (`remote.rs`), then mapped into the mstsc client area.
//! HANDY_REMOTE_CARET=0 turns that off; HANDY_REMOTE_CARET=host:port changes the helper address.

use std::ffi::c_void;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use windows::core::{Interface, BOOL};
use windows::Win32::Foundation::{HWND, POINT, RECT};
use windows::Win32::Graphics::Gdi::ClientToScreen;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
};
use windows::Win32::System::Ole::{
    SafeArrayAccessData, SafeArrayDestroy, SafeArrayGetUBound, SafeArrayUnaccessData,
};
use windows::Win32::System::Variant::VARIANT;
use windows::Win32::UI::Accessibility::{
    AccessibleObjectFromWindow, CUIAutomation, IAccessible, IUIAutomation,
    IUIAutomationTextPattern, IUIAutomationTextPattern2, IUIAutomationTextRange,
    TextPatternRangeEndpoint_Start, TextUnit_Character, UIA_TextPattern2Id, UIA_TextPatternId,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetClassNameW, GetClientRect, GetForegroundWindow, GetGUIThreadInfo, GetWindowRect,
    GetWindowThreadProcessId, GUITHREADINFO, OBJID_CARET,
};

/// Caret rectangle in physical screen pixels.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Caret {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

/// The caret the overlay was last placed under (None: placed at the screen edge).
static PLACED: Mutex<Option<Caret>> = Mutex::new(None);

pub fn enabled() -> bool {
    std::env::var("HANDY_OVERLAY_CARET")
        .map(|v| v != "0")
        .unwrap_or(true)
}

pub fn remember(c: Option<Caret>) {
    if let Ok(mut p) = PLACED.lock() {
        *p = c;
    }
}

pub fn placed() -> Option<Caret> {
    PLACED.lock().ok().and_then(|p| *p)
}

/// Whether the overlay should move: the caret changed line, or left the overlay's horizontal span.
pub fn moved(now: Caret, placed: Option<Caret>, overlay_left: i32, overlay_right: i32) -> bool {
    match placed {
        None => true,
        Some(p) => {
            (now.bottom - p.bottom).abs() > 4 || now.left < overlay_left || now.left > overlay_right
        }
    }
}

/// The foreground app's caret, or None if it has none (or it could not be read within `timeout`).
pub fn locate(timeout: Duration) -> Option<Caret> {
    let (tx, rx) = mpsc::channel();
    std::thread::Builder::new()
        .name("caret".into())
        .spawn(move || {
            let _ = tx.send(locate_now());
        })
        .ok()?;
    rx.recv_timeout(timeout).ok().flatten()
}

fn locate_now() -> Option<Caret> {
    unsafe {
        let fg = GetForegroundWindow();
        if fg.is_invalid() {
            return None;
        }
        if is_remote_desktop(fg) {
            return remote_desktop_caret(fg);
        }
        let mut win = RECT::default();
        GetWindowRect(fg, &mut win).ok()?;
        let tid = GetWindowThreadProcessId(fg, None);
        let mut gi = GUITHREADINFO {
            cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
            ..Default::default()
        };
        let gui_ok = GetGUIThreadInfo(tid, &mut gi).is_ok();
        let inside = |c: Caret| {
            c.bottom > c.top
                && c.left >= win.left - 8
                && c.left <= win.right + 8
                && c.top >= win.top - 8
                && c.bottom <= win.bottom + 8
        };
        if gui_ok && !gi.hwndCaret.is_invalid() {
            if let Some(c) = system_caret(gi.hwndCaret, gi.rcCaret).filter(|&c| inside(c)) {
                return Some(c);
            }
        }
        let focus = if gui_ok && !gi.hwndFocus.is_invalid() {
            gi.hwndFocus
        } else {
            fg
        };
        let _com = ComGuard::new();
        if let Some(c) = msaa_caret(focus)
            .or_else(|| msaa_caret(fg))
            .filter(|&c| inside(c))
        {
            return Some(c);
        }
        uia_caret().filter(|&c| inside(c))
    }
}

struct ComGuard(bool);

impl ComGuard {
    unsafe fn new() -> Self {
        ComGuard(CoInitializeEx(None, COINIT_MULTITHREADED).is_ok())
    }
}

impl Drop for ComGuard {
    fn drop(&mut self) {
        if self.0 {
            unsafe { CoUninitialize() };
        }
    }
}

unsafe fn system_caret(hwnd: HWND, r: RECT) -> Option<Caret> {
    if r.bottom <= r.top {
        return None;
    }
    let mut a = POINT {
        x: r.left,
        y: r.top,
    };
    let mut b = POINT {
        x: r.right,
        y: r.bottom,
    };
    if !ClientToScreen(hwnd, &mut a).as_bool() || !ClientToScreen(hwnd, &mut b).as_bool() {
        return None;
    }
    Some(Caret {
        left: a.x,
        top: a.y,
        right: b.x,
        bottom: b.y,
    })
}

unsafe fn msaa_caret(hwnd: HWND) -> Option<Caret> {
    let mut p: *mut c_void = std::ptr::null_mut();
    AccessibleObjectFromWindow(hwnd, OBJID_CARET.0 as u32, &IAccessible::IID, &mut p).ok()?;
    if p.is_null() {
        return None;
    }
    let acc = IAccessible::from_raw(p);
    let (mut l, mut t, mut w, mut h) = (0i32, 0i32, 0i32, 0i32);
    let me = VARIANT::from(0i32); // CHILDID_SELF
    acc.accLocation(&mut l, &mut t, &mut w, &mut h, &me).ok()?;
    if h <= 0 || (l == 0 && t == 0) {
        return None;
    }
    Some(Caret {
        left: l,
        top: t,
        right: l + w.max(1),
        bottom: t + h,
    })
}

unsafe fn uia_caret() -> Option<Caret> {
    let uia: IUIAutomation = CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER).ok()?;
    let el = uia.GetFocusedElement().ok()?;
    let range: IUIAutomationTextRange =
        if let Ok(p2) = el.GetCurrentPatternAs::<IUIAutomationTextPattern2>(UIA_TextPattern2Id) {
            let mut active = BOOL(0);
            p2.GetCaretRange(&mut active).ok()?
        } else {
            let p: IUIAutomationTextPattern = el.GetCurrentPatternAs(UIA_TextPatternId).ok()?;
            let sel = p.GetSelection().ok()?;
            if sel.Length().ok()? < 1 {
                return None;
            }
            sel.GetElement(0).ok()?
        };
    if let Some(c) = range_rect(&range, false) {
        return Some(c);
    }
    // A collapsed range has no rectangle: measure the character before the caret and take its right edge.
    let prev = range.Clone().ok()?;
    if prev
        .MoveEndpointByUnit(TextPatternRangeEndpoint_Start, TextUnit_Character, -1)
        .is_ok()
    {
        if let Some(c) = range_rect(&prev, true) {
            return Some(c);
        }
    }
    let next = range.Clone().ok()?;
    next.ExpandToEnclosingUnit(TextUnit_Character).ok()?;
    range_rect(&next, false)
}

/// Last bounding rectangle of a text range; `right_edge` collapses it to its right side.
unsafe fn range_rect(r: &IUIAutomationTextRange, right_edge: bool) -> Option<Caret> {
    let sa = r.GetBoundingRectangles().ok()?;
    if sa.is_null() {
        return None;
    }
    let mut out = None;
    if let Ok(ub) = SafeArrayGetUBound(sa, 1) {
        let n = (ub + 1) as usize;
        let mut data: *mut c_void = std::ptr::null_mut();
        if n >= 4 && SafeArrayAccessData(sa, &mut data).is_ok() {
            let v = std::slice::from_raw_parts(data as *const f64, n);
            let k = n / 4 * 4 - 4;
            let (l, t, w, h) = (v[k], v[k + 1], v[k + 2], v[k + 3]);
            if h > 0.0 {
                let left = if right_edge { l + w } else { l };
                out = Some(Caret {
                    left: left.round() as i32,
                    top: t.round() as i32,
                    right: (left + 1.0).round() as i32,
                    bottom: (t + h).round() as i32,
                });
            }
            let _ = SafeArrayUnaccessData(sa);
        }
    }
    let _ = SafeArrayDestroy(sa);
    out
}

// ---- Remote Desktop: caret of the app inside the mstsc window, from caret_helper.exe over ssh ----

const RDP_CLASS: &str = "TscShellContainerClass";
const TUNNEL_RETRY: Duration = Duration::from_secs(20);

struct Tunnel {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Drop for Tunnel {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

static TUNNEL: Mutex<Option<Tunnel>> = Mutex::new(None);
static TUNNEL_RETRY_AT: Mutex<Option<Instant>> = Mutex::new(None);

/// Remote caret and virtual-screen bounds, in the remote session's physical pixels.
struct RemoteCaret {
    l: i32,
    t: i32,
    r: i32,
    b: i32,
    vx: i32,
    vy: i32,
    vw: i32,
    vh: i32,
}

unsafe fn is_remote_desktop(hwnd: HWND) -> bool {
    let mut buf = [0u16; 64];
    let n = GetClassNameW(hwnd, &mut buf);
    n > 0 && String::from_utf16_lossy(&buf[..n as usize]) == RDP_CLASS
}

/// The remote caret mapped into the mstsc client area (scaled when the session is smart-sized).
unsafe fn remote_desktop_caret(fg: HWND) -> Option<Caret> {
    let r = remote_query()?;
    let mut client = RECT::default();
    GetClientRect(fg, &mut client).ok()?;
    let mut origin = POINT { x: 0, y: 0 };
    if !ClientToScreen(fg, &mut origin).as_bool() {
        return None;
    }
    let (cw, ch) = (
        (client.right - client.left) as f64,
        (client.bottom - client.top) as f64,
    );
    if r.vw <= 0 || r.vh <= 0 || cw <= 0.0 || ch <= 0.0 {
        return None;
    }
    let (sx, sy) = (cw / r.vw as f64, ch / r.vh as f64);
    let mx = |x: i32| origin.x + ((x - r.vx) as f64 * sx).round() as i32;
    let my = |y: i32| origin.y + ((y - r.vy) as f64 * sy).round() as i32;
    let left = mx(r.l);
    Some(Caret {
        left,
        top: my(r.t),
        right: mx(r.r).max(left + 1),
        bottom: my(r.b),
    })
}

/// `ssh -W <helper>` from the remote recognition settings (same host, key and options as the worker).
fn open_tunnel() -> Option<Tunnel> {
    let mut child = crate::remote::caret_spec()?
        .command()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let stdin = child.stdin.take()?;
    let stdout = BufReader::new(child.stdout.take()?);
    Some(Tunnel {
        child,
        stdin,
        stdout,
    })
}

fn tunnel_backoff() {
    if let Ok(mut r) = TUNNEL_RETRY_AT.lock() {
        *r = Some(Instant::now() + TUNNEL_RETRY);
    }
}

/// One request on the persistent tunnel (~10 ms once open; opening takes ~0.5-1 s, which the caller's
/// timeout absorbs: the overlay starts at the screen edge and follow_caret moves it once connected).
fn remote_query() -> Option<RemoteCaret> {
    let mut guard = TUNNEL.try_lock().ok()?;
    let line = tunnel_request(&mut guard, b"q\n")?;
    let v: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    if v.get("ok").and_then(|o| o.as_i64()) != Some(1) {
        return None;
    }
    let g = |k: &str| v.get(k).and_then(|x| x.as_i64()).map(|x| x as i32);
    Some(RemoteCaret {
        l: g("l")?,
        t: g("t")?,
        r: g("r")?,
        b: g("b")?,
        vx: g("vx")?,
        vy: g("vy")?,
        vw: g("vw")?,
        vh: g("vh")?,
    })
}

/// One request line on the persistent tunnel, opening it first when needed; returns the reply line.
fn tunnel_request(guard: &mut Option<Tunnel>, request: &[u8]) -> Option<String> {
    if guard.is_none() {
        let waiting = TUNNEL_RETRY_AT
            .lock()
            .ok()
            .and_then(|r| *r)
            .is_some_and(|t| Instant::now() < t);
        if waiting {
            return None;
        }
        *guard = open_tunnel();
        if guard.is_none() {
            tunnel_backoff();
            return None;
        }
        log::info!("remote caret: tunnel opened");
    }
    let t = guard.as_mut()?;
    let mut line = String::new();
    let ok = t
        .stdin
        .write_all(request)
        .and_then(|_| t.stdin.flush())
        .is_ok()
        && t.stdout
            .read_line(&mut line)
            .map(|n| n > 0)
            .unwrap_or(false);
    if !ok {
        *guard = None;
        tunnel_backoff();
        log::info!(
            "remote caret: helper not reachable, retrying in {}s",
            TUNNEL_RETRY.as_secs()
        );
        return None;
    }
    Some(line)
}

/// With `rdp_dictation` on and a Remote Desktop window in front, asks the caret helper to toggle
/// Handy inside the remote session (it records the redirected microphone and pastes there).
/// False when it did not happen (setting off, other window, helper unreachable or too old), so the
/// caller dictates locally as before.
pub fn forward_toggle_to_remote_desktop() -> bool {
    let ok = forward_to_remote_desktop(b"t\n", "toggled");
    if let Some((ok, reply)) = ok.as_ref() {
        log::info!(
            "remote desktop dictation: toggle {}",
            if *ok {
                "forwarded".to_string()
            } else {
                format!("not forwarded ({}), dictating here", reply.trim())
            }
        );
    }
    ok.is_some_and(|(ok, _)| ok)
}

/// Same gate as dictation: opens voice feedback (`report`, `review`, `guide`) of the Handy inside
/// the Remote Desktop session in front, whose last dictation is the one the user means.
pub fn forward_feedback_to_remote_desktop(action: &str) -> bool {
    let request = format!("f {action}\n");
    let ok = forward_to_remote_desktop(request.as_bytes(), "forwarded");
    if let Some((ok, reply)) = ok.as_ref() {
        log::info!(
            "remote desktop feedback: {action} {}",
            if *ok {
                "forwarded".to_string()
            } else {
                format!("not forwarded ({}), opening here", reply.trim())
            }
        );
    }
    ok.is_some_and(|(ok, _)| ok)
}

/// None when not attempted (rdp_dictation off or no Remote Desktop window in front); otherwise
/// whether the helper answered with `"<key>":1`, plus its reply line.
fn forward_to_remote_desktop(request: &[u8], key: &str) -> Option<(bool, String)> {
    if !crate::remote::rdp_dictation() {
        return None;
    }
    let rdp = unsafe {
        let fg = GetForegroundWindow();
        !fg.is_invalid() && is_remote_desktop(fg)
    };
    if !rdp {
        return None;
    }
    let mut guard = TUNNEL.lock().ok()?;
    let reply = tunnel_request(&mut guard, request).unwrap_or_default();
    let ok = serde_json::from_str::<serde_json::Value>(reply.trim())
        .ok()
        .is_some_and(|v| v.get(key).and_then(|o| o.as_i64()) == Some(1));
    Some((ok, reply))
}

/// Opens the tunnel while a Remote Desktop window is in front, so the first dictation there already
/// lands under the remote caret.
pub fn start_remote_prewarm() {
    if !enabled() {
        return;
    }
    let _ = std::thread::Builder::new()
        .name("remote-caret".into())
        .spawn(|| loop {
            std::thread::sleep(Duration::from_secs(2));
            let rdp = unsafe {
                let fg = GetForegroundWindow();
                !fg.is_invalid() && is_remote_desktop(fg)
            };
            if rdp
                && TUNNEL.lock().map(|t| t.is_none()).unwrap_or(false)
                && crate::remote::caret_spec().is_some()
            {
                let _ = remote_query();
            }
        });
}
