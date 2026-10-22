# -*- coding: utf-8 -*-
"""系统托盘图标（纯 ctypes 调 Win32，不引入第三方库）。

右键菜单：显示主界面 / 小播放器（播放·暂停、上一首、下一首）/ 各功能页 / 退出。
左键双击：打开主界面。

为什么不用 pystray：本项目后端只用标准库，多引一个第三方包会让打包和
依赖检查都变复杂；Shell_NotifyIcon 本身用 ctypes 就能调。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
from pathlib import Path

# ---------------------------------------------------------------- Win32 常量

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_APP = 0x8000
WM_TRAY = WM_APP + 20
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
MF_CHECKED = 0x0008

TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

HWND_MESSAGE = -3
IDI_APPLICATION = 32512

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# 必须声明 argtypes：lparam 是有符号的 LPARAM，不声明时 ctypes 会把
# 某些高位为 1 的值按无符号处理，抛 "int too long to convert"，
# 窗口过程里每个未处理的消息都会刷一条异常。
LRESULT = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.TrackPopupMenuEx.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                    wt.HWND, wt.LPVOID]
user32.TrackPopupMenuEx.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = LRESULT
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                              ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadIconW.restype = wt.HICON
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class Tray:
    """托盘图标 + 菜单。构造后调用 start() 起线程。"""

    # (label, hash, 是否属于附加工具箱)
    PAGES = [
        ("项目", "#projects", False),
        ("解析", "#home", True),
        ("音乐", "#music", True),
        ("播放", "#player", True),
        ("剪辑", "#clip", True),
        ("文档", "#doc", False),
        ("参考文献", "#papers", False),
        ("图片", "#image", True),
        ("记录", "#tasks", False),
        ("设置", "#settings", False),
    ]

    def __init__(self, window_getter=None, icon_path=None, on_quit=None, tooltip="Makise Toolkit"):
        self._window_getter = window_getter or (lambda: None)
        self._icon_path = str(icon_path) if icon_path else ""
        self._on_quit = on_quit
        self._tooltip = tooltip
        self._hwnd = None
        self._nid = None
        self._thread = None
        self._wndproc = None
        self._paused = False

    # ------------------------------------------------------------ 窗口操作
    def _window(self):
        try:
            return self._window_getter()
        except Exception:
            return None

    def _show_window(self):
        win = self._window()
        if not win:
            return
        for name in ("show", "restore"):
            fn = getattr(win, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def _navigate(self, hash_part: str):
        win = self._window()
        self._show_window()
        if not win:
            return
        js = "location.hash=" + repr(hash_part)
        try:
            win.evaluate_js(js)
        except Exception:
            pass

    def _open_dir(self, which: str) -> None:
        """打开安装目录或下载目录。失败不弹窗，只写日志。"""
        import os
        try:
            from .config import BASE_DIR, DATA_DIR, load_settings
            if which == "install":
                target = BASE_DIR
            else:
                from pathlib import Path
                s = load_settings()
                target = Path(s.download_dir) if s.download_dir else (BASE_DIR / "downloads")
                target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
        except Exception as exc:
            try:
                from .log import log_error
                log_error("tray.open_dir:" + which, exc)
            except Exception:
                pass

    def _open_log(self) -> None:
        """打开 data/error.log。没有就先生成一个带说明的文件。"""
        import os
        try:
            from .config import DATA_DIR
            path = DATA_DIR / "error.log"
            if not path.exists():
                path.write_text("（目前没有记录到错误）\n", encoding="utf-8")
            os.startfile(str(path))
        except Exception as exc:
            try:
                from .log import log_error
                log_error("tray.open_log", exc)
            except Exception:
                pass

    def _player(self, action: str, arg: str = ""):
        win = self._window()
        if not win:
            return
        js = ("try{window.__vgTray&&window.__vgTray." + action + "(" + arg + ")}catch(e){}")
        try:
            win.evaluate_js(js)
        except Exception:
            pass

    # ------------------------------------------------------------ 菜单
    def _extras_on(self) -> bool:
        """附加工具箱开着吗。读配置而不是缓存：用户改完设置，下一次右键就生效。"""
        try:
            from .config import load_settings
            return load_settings().extras_enabled is True
        except Exception:
            return False

    def _build_menu(self) -> int:
        menu = user32.CreatePopupMenu()
        extras = self._extras_on()
        items = [("显示主界面", 100), (None, 0)]
        if extras:
            # 播放控制是给音乐用的，工具箱关着时整段不出现
            items += [("播放 / 暂停", 201), ("上一首", 202), ("下一首", 203), (None, 0),
                      ("播放方式", 0),          # 小标题，灰掉不可点
                      ("　顺序播放", 211), ("　列表循环", 212),
                      ("　单曲循环", 213), ("　乱序播放", 214), (None, 0)]
        for label, cmd in items:
            if label is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            elif cmd == 0:
                # 标题行：灰色不可点
                user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, label)
            else:
                user32.AppendMenuW(menu, MF_STRING, cmd, label)
        # 页面跳转单独一组，加个灰色小标题，免得和播放控制混在一起
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, "打开页面")
        pages = [item for item in self.PAGES if extras or not item[2]]
        self._menu_pages = pages        # 命令号要跟这次真正放进去的列表对齐
        for index, (label, _hash, _x) in enumerate(pages):
            # 关掉工具箱时这一页只剩文献，名字跟着界面一起改
            shown = "下载过的论文" if (label == "记录" and not extras) else label
            user32.AppendMenuW(menu, MF_STRING, 300 + index, "　" + shown)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        # 系统：方便出问题时自查，不用去翻安装目录
        user32.AppendMenuW(menu, MF_STRING, 901, "打开安装目录")
        if extras:
            user32.AppendMenuW(menu, MF_STRING, 902, "打开下载目录")
        user32.AppendMenuW(menu, MF_STRING, 903, "查看错误日志")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 900, "退出")
        return menu

    def _handle_cmd(self, cmd: int) -> None:
        if cmd == 100:
            self._show_window()
        elif cmd == 201:
            self._player("toggle")
        elif cmd == 202:
            self._player("prev")
        elif cmd == 203:
            self._player("next")
        elif cmd in (211, 212, 213, 214):
            mode = {211: "off", 212: "all", 213: "one", 214: "shuffle"}[cmd]
            self._player("setLoop", "'" + mode + "'")
        elif 300 <= cmd < 300 + len(getattr(self, "_menu_pages", self.PAGES)):
            self._navigate(self._menu_pages[cmd - 300][1])
        elif cmd == 901:
            self._open_dir("install")
        elif cmd == 902:
            self._open_dir("download")
        elif cmd == 903:
            self._open_log()
        elif cmd == 900:
            self.quit()

    def quit(self) -> None:
        """退出：先让主程序收尾（关窗口、停服务），再结束进程。"""
        try:
            if self._on_quit:
                self._on_quit()
        except Exception:
            pass
        try:
            self._remove_icon()
        except Exception:
            pass
        # 服务是 daemon 线程，进程退出即结束
        os._exit(0)

    # ------------------------------------------------------------ 图标
    def _load_icon(self) -> int:
        if self._icon_path and os.path.isfile(self._icon_path):
            handle = user32.LoadImageW(None, self._icon_path, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        # 打包成 exe 后 build-tools/app.ico 不在包里，改成从 exe 自身抠图标
        try:
            import sys
            if getattr(sys, "frozen", False) and sys.executable:
                large = wt.HICON()
                small = wt.HICON()
                user32.PrivateExtractIconsW.argtypes = [
                    wt.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.POINTER(wt.HICON), ctypes.POINTER(wt.UINT),
                    wt.UINT, wt.UINT]
                user32.PrivateExtractIconsW.restype = wt.UINT
                count = user32.PrivateExtractIconsW(
                    sys.executable, 0, 32, 32,
                    ctypes.byref(large), ctypes.byref(wt.UINT()), 1, 0)
                if count and large.value:
                    return large.value
        except Exception:
            pass
        # 第二个参数是 MAKEINTRESOURCE(IDI_APPLICATION)，不是字符串指针
        user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
        user32.LoadIconW.restype = wt.HICON
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _add_icon(self) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self._tooltip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._nid = nid

    def _remove_icon(self) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    # ------------------------------------------------------------ 消息循环
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_RBUTTONUP:
                menu = self._build_menu()
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                # 必须 SetForegroundWindow，否则菜单点外面不会消失
                user32.SetForegroundWindow(hwnd)
                cmd = user32.TrackPopupMenuEx(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                              pt.x, pt.y, hwnd, None)
                user32.PostMessageW(hwnd, 0, 0, 0)
                user32.DestroyMenu(menu)
                if cmd:
                    self._handle_cmd(cmd)
                return 0
            if event == WM_LBUTTONDBLCLK:
                self._show_window()
                return 0
            return 0
        if msg == WM_COMMAND:
            self._handle_cmd(int(wparam) & 0xFFFF)
            return 0
        if msg == WM_DESTROY:
            self._remove_icon()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._wnd_proc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = instance
        wc.lpszClassName = "MakiseTrayWnd"
        user32.RegisterClassW(ctypes.byref(wc))
        # HWND_MESSAGE：只用来收消息的隐藏窗口，不出现在任务栏
        self._hwnd = user32.CreateWindowExW(0, "MakiseTrayWnd", "MakiseTray",
                                            0, 0, 0, 0, 0,
                                            ctypes.c_void_p(HWND_MESSAGE), None,
                                            instance, None)
        if not self._hwnd:
            return
        self._add_icon()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def start(self) -> None:
        """在后台线程起托盘（webview.start() 会阻塞主线程，不能占用它）。"""
        if os.name != "nt":
            return
        if self._thread is not None:
            return
        # 深色菜单要在建菜单之前就设好，否则第一版菜单还是白的
        enable_dark_menus()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()


def enable_dark_menus() -> bool:
    """让系统弹出菜单跟着用深色，和主界面的深色主题一致。

    原生托盘菜单是 Windows 自己画的，**没法**套成 Material 3（不能改颜色、
    圆角、字体）。能做的只有这一件：通过未文档化的 SetPreferredAppMode
    让系统把菜单画成深色，至少不再是一片刺眼的白。

    这两个导出只有序号没有名字，只能用 GetProcAddress 拿地址；
    序号在不同 Windows 版本上可能变，所以全部包在 try 里，失败就当没这回事。
    """
    try:
        uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.GetProcAddress.restype = ctypes.c_void_p
        module = ctypes.c_void_p(uxtheme._handle)
        addr = k32.GetProcAddress(module, ctypes.c_void_p(135))   # SetPreferredAppMode
        if not addr:
            return False
        ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)(addr)(2)  # 2 = ForceDark
        flush = k32.GetProcAddress(module, ctypes.c_void_p(136))  # FlushMenuThemes
        if flush:
            ctypes.WINFUNCTYPE(None)(flush)()
        return True
    except Exception:
        return False


def app_icon_path() -> str:
    """找托盘/窗口用的 .ico：源码模式下在 build-tools 里。"""
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / "build-tools" / "app.ico", here / "app.ico"):
        if candidate.is_file():
            return str(candidate)
    return ""
