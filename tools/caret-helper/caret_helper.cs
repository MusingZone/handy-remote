// Caret helper for dictating inside a Remote Desktop window: runs in the user's interactive session on
// this machine and answers one JSON line per request line on 127.0.0.1:47823 (or --port N /
// HANDY_REMOTE_CARET_PORT) with the foreground app's
// text caret in physical pixels plus the virtual-screen bounds, so the laptop's Handy (reaching it through
// `ssh -W 127.0.0.1:47823`) can place its overlay under the remote caret. Same lookup order as Handy's
// caret.rs: Win32 system caret, MSAA OBJID_CARET, UI Automation selection. Build: build.cmd.
// The request line "t" instead toggles dictation of the Handy running in this session (what
// `handy --toggle-transcription` does), so the laptop can hand dictation inside the Remote Desktop
// window to it (remote setting rdp_dictation); answer {"ok":1,"toggled":1} or {"ok":0,"toggled":0,...}.
// "f <action>" (report, review, guide) opens that Handy's voice feedback; answer {"ok":1,"forwarded":1}.
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;
using System.Windows.Automation.Text;

static class CaretHelper
{
    const int DefaultPort = 47823;

    [StructLayout(LayoutKind.Sequential)] struct RECT { public int l, t, r, b; }
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int x, y; }
    [StructLayout(LayoutKind.Sequential)]
    struct GUITHREADINFO
    {
        public int cbSize, flags;
        public IntPtr hwndActive, hwndFocus, hwndCapture, hwndMenuOwner, hwndMoveSize, hwndCaret;
        public RECT rcCaret;
    }

    [DllImport("user32.dll")] static extern IntPtr SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, IntPtr pid);
    [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint tid, ref GUITHREADINFO info);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hwnd, ref POINT p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out RECT r);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetClassName(IntPtr hwnd, StringBuilder name, int max);
    [DllImport("oleacc.dll")]
    static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid,
        [MarshalAs(UnmanagedType.IUnknown)] out object obj);

    [StructLayout(LayoutKind.Sequential)]
    struct COPYDATASTRUCT { public IntPtr dwData; public int cbData; public IntPtr lpData; }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr FindWindow(string cls, string name);
    [DllImport("user32.dll")]
    static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr wp, ref COPYDATASTRUCT lp,
        uint flags, uint timeoutMs, out IntPtr result);

    // Fork and upstream app identifiers; tauri-plugin-single-instance names its message window
    // "<id>-siw" (class "<id>-sic") and takes "cwd|exe|args…" in WM_COPYDATA with dwData 1542.
    static readonly string[] HandyIds = { "com.musingzone.handy-remote", "com.pais.handy" };

    static string Toggle()
    {
        string r = SendToHandy("--toggle-transcription");
        return r == null ? "{\"ok\":1,\"toggled\":1}" : "{\"ok\":0,\"toggled\":0,\"why\":\"" + r + "\"}";
    }

    // "f <action>": the voice-feedback window of this session's Handy (handy --voice-feedback).
    static string Feedback(string action)
    {
        if (Array.IndexOf(FeedbackActions, action) < 0)
            return "{\"ok\":0,\"forwarded\":0,\"why\":\"unknown action\"}";
        string r = SendToHandy("--voice-feedback|" + action);
        return r == null ? "{\"ok\":1,\"forwarded\":1}" : "{\"ok\":0,\"forwarded\":0,\"why\":\"" + r + "\"}";
    }

    static readonly string[] FeedbackActions = { "report", "review", "guide" };

    // Null when delivered, otherwise why not. args is "|"-separated.
    static string SendToHandy(string args)
    {
        foreach (string id in HandyIds)
        {
            IntPtr hwnd = FindWindow(id + "-sic", id + "-siw");
            if (hwnd == IntPtr.Zero) continue;
            byte[] data = Encoding.UTF8.GetBytes("C:\\|handy.exe|" + args + "\0");
            IntPtr buf = Marshal.AllocHGlobal(data.Length);
            try
            {
                Marshal.Copy(data, 0, buf, data.Length);
                var cds = new COPYDATASTRUCT { dwData = new IntPtr(1542), cbData = data.Length, lpData = buf };
                IntPtr result;
                // WM_COPYDATA, SMTO_ABORTIFHUNG
                if (SendMessageTimeout(hwnd, 0x004A, IntPtr.Zero, ref cds, 0x0002, 2000, out result) != IntPtr.Zero)
                    return null;
                return "no answer from " + id;
            }
            finally { Marshal.FreeHGlobal(buf); }
        }
        return "Handy is not running in this session";
    }

    static void Main(string[] args)
    {
        int port = DefaultPort;
        int i = Array.IndexOf(args, "--port");
        string env = Environment.GetEnvironmentVariable("HANDY_REMOTE_CARET_PORT");
        if (i >= 0 && i + 1 < args.Length) int.TryParse(args[i + 1], out port);
        else if (!string.IsNullOrEmpty(env)) int.TryParse(env, out port);
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch { SetProcessDPIAware(); }
        TcpListener listener;
        try
        {
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
        }
        catch (SocketException) { return; } // already running
        while (true)
        {
            TcpClient client = listener.AcceptTcpClient();
            new Thread(() => Serve(client)) { IsBackground = true }.Start();
        }
    }

    static void Serve(TcpClient client)
    {
        using (client)
        {
            try
            {
                NetworkStream s = client.GetStream();
                var reader = new StreamReader(s, Encoding.ASCII);
                var writer = new StreamWriter(s, Encoding.ASCII) { AutoFlush = true, NewLine = "\n" };
                string request;
                while ((request = reader.ReadLine()) != null)
                {
                    if (request.Trim() == "t")
                    {
                        writer.WriteLine(Toggle());
                        continue;
                    }
                    if (request.StartsWith("f "))
                    {
                        writer.WriteLine(Feedback(request.Substring(2).Trim()));
                        continue;
                    }
                    var task = Task.Run(() => Locate());
                    string reply = task.Wait(300) && task.Result != null
                        ? task.Result
                        : "{\"ok\":0,\"fg\":\"" + ForegroundClass() + "\",\"why\":\"" + diag + "\"}";
                    writer.WriteLine(reply);
                }
            }
            catch (IOException) { }
            catch (ObjectDisposedException) { }
        }
    }

    static volatile string diag = "";

    static string Locate()
    {
        diag = "";
        IntPtr fg = GetForegroundWindow();
        if (fg == IntPtr.Zero) return null;
        RECT win;
        if (!GetWindowRect(fg, out win)) return null;
        var gi = new GUITHREADINFO { cbSize = Marshal.SizeOf(typeof(GUITHREADINFO)) };
        bool guiOk = GetGUIThreadInfo(GetWindowThreadProcessId(fg, IntPtr.Zero), ref gi);
        RECT c;
        if (guiOk && gi.hwndCaret != IntPtr.Zero && SystemCaret(gi.hwndCaret, gi.rcCaret, out c) && Inside(c, win))
            return Json(c);
        IntPtr focus = guiOk && gi.hwndFocus != IntPtr.Zero ? gi.hwndFocus : fg;
        diag = "gui=" + guiOk + " caret=" + gi.hwndCaret + " focus=" + gi.hwndFocus;
        if ((MsaaCaret(focus, out c) || MsaaCaret(fg, out c)) && Inside(c, win)) return Json(c);
        diag += " msaa=" + c.l + "/" + c.t + "/" + c.r + "/" + c.b;
        if (UiaCaret(out c) && Inside(c, win)) return Json(c);
        diag += " uia=" + c.l + "/" + c.t + "/" + c.r + "/" + c.b + " " + uiaDiag;
        return null;
    }

    static string ForegroundClass()
    {
        var sb = new StringBuilder(128);
        GetClassName(GetForegroundWindow(), sb, sb.Capacity);
        return sb.ToString().Replace("\\", "").Replace("\"", "");
    }

    static bool Inside(RECT c, RECT w)
    {
        return c.b > c.t && c.l >= w.l - 8 && c.l <= w.r + 8 && c.t >= w.t - 8 && c.b <= w.b + 8;
    }

    static string Json(RECT c)
    {
        return string.Format("{{\"ok\":1,\"l\":{0},\"t\":{1},\"r\":{2},\"b\":{3},\"vx\":{4},\"vy\":{5},\"vw\":{6},\"vh\":{7}}}",
            c.l, c.t, c.r, c.b, GetSystemMetrics(76), GetSystemMetrics(77), GetSystemMetrics(78), GetSystemMetrics(79));
    }

    static bool SystemCaret(IntPtr hwnd, RECT r, out RECT c)
    {
        c = new RECT();
        if (r.b <= r.t) return false;
        var a = new POINT { x = r.l, y = r.t };
        var b = new POINT { x = r.r, y = r.b };
        if (!ClientToScreen(hwnd, ref a) || !ClientToScreen(hwnd, ref b)) return false;
        c = new RECT { l = a.x, t = a.y, r = b.x, b = b.y };
        return true;
    }

    static bool MsaaCaret(IntPtr hwnd, out RECT c)
    {
        c = new RECT();
        try
        {
            Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
            object o;
            if (AccessibleObjectFromWindow(hwnd, 0xFFFFFFF8, ref iid, out o) != 0 || o == null) return false;
            var acc = (Accessibility.IAccessible)o;
            int l, t, w, h;
            acc.accLocation(out l, out t, out w, out h, 0);
            if (h <= 0 || (l == 0 && t == 0)) return false;
            c = new RECT { l = l, t = t, r = l + Math.Max(w, 1), b = t + h };
            return true;
        }
        catch { return false; }
    }

    static volatile string uiaDiag = "";

    static bool UiaCaret(out RECT c)
    {
        c = new RECT();
        try
        {
            AutomationElement el = AutomationElement.FocusedElement;
            object p;
            uiaDiag = el == null ? "nofocus" : el.Current.ControlType.ProgrammaticName + ":" + el.Current.ClassName;
            if (el == null || !el.TryGetCurrentPattern(TextPattern.Pattern, out p)) return false;
            TextPatternRange[] sel = ((TextPattern)p).GetSelection();
            uiaDiag += " sel=" + (sel == null ? -1 : sel.Length);
            if (sel == null || sel.Length < 1) return false;
            if (RangeRect(sel[0], false, out c)) return true;
            // A collapsed range has no rectangle: take the right edge of the character before it.
            TextPatternRange prev = sel[0].Clone();
            prev.MoveEndpointByUnit(TextPatternRangeEndpoint.Start, TextUnit.Character, -1);
            if (RangeRect(prev, true, out c)) return true;
            TextPatternRange next = sel[0].Clone();
            next.ExpandToEnclosingUnit(TextUnit.Character);
            return RangeRect(next, false, out c);
        }
        catch { return false; }
    }

    static bool RangeRect(TextPatternRange r, bool rightEdge, out RECT c)
    {
        c = new RECT();
        System.Windows.Rect[] rects = r.GetBoundingRectangles();
        if (rects == null || rects.Length == 0) return false;
        System.Windows.Rect last = rects[rects.Length - 1];
        if (last.IsEmpty || last.Height <= 0) return false;
        double left = rightEdge ? last.Right : last.Left;
        c = new RECT
        {
            l = (int)Math.Round(left), t = (int)Math.Round(last.Top),
            r = (int)Math.Round(left + 1), b = (int)Math.Round(last.Bottom)
        };
        return true;
    }
}
