#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FileTransferOne - Optimized File Transfer Server + GUI Client
- Streamed upload (low memory)
- Robust text+binary protocol with length-prefix
- Drag & Drop support via tkinterdnd2 (optional)
- Thread-safe GUI logging and progress
- System Tray integration (pystray): right-click quick actions, show/hide, start/stop, quit
- Minimize to tray on close
- Auto-start on Windows (Registry Run) with background + auto-start server
- Background operation via CLI: --background, --start-server
"""

import os
import sys
import socket
import threading
import time
import json
import logging
import queue
import platform
import subprocess
from datetime import datetime
from typing import Optional, Callable, List, Tuple

# GUI
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional drag & drop
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES  # pip install tkinterdnd2
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

# System tray
try:
    import pystray  # pip install pystray
    from PIL import Image, ImageDraw  # pip install pillow
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

APP_NAME = "FileTransferOne"
VERSION = "2.2"
# Tự động xác định thư mục chứa file exe-python
if getattr(sys, 'frozen', False):
    # PyInstaller exe - lưu file tại thư mục chứa file exe
    UPLOAD_DIR = os.path.join(os.path.dirname(sys.executable), "server_files")
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), "config.json")
    LOG_DIR = os.path.join(os.path.dirname(sys.executable), "logs")
else:
    # Python script - lưu file tại thư mục hiện tại
    UPLOAD_DIR = "server_files"
    CONFIG_FILE = "config.json"
    LOG_DIR = "logs"

DEFAULT_PORT = 9100
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_FILE_SIZE = 1024 * 1024 * 1024 * 10  # 10 GB
LISTEN_BACKLOG = 128

# ===== Logging =====
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = os.path.join(LOG_DIR, f"{timestamp}_{APP_NAME}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    logging.info("=== %s v%s starting ===", APP_NAME, VERSION)

setup_logging()

# ===== Utils =====
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sanitize_filename(name: str) -> str:
    base = os.path.basename(name)
    forbidden = '<>:"/\\|?*\n\r\t'
    cleaned = "".join(c for c in base if c not in forbidden)
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "unnamed"
    return cleaned[:255]

def make_unique_path(directory: str, filename: str) -> str:
    name, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    idx = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{name} ({idx}){ext}")
        idx += 1
    return candidate

def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

def human_bytes(num: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024.0 or unit == "TB":
            return f"{num:.2f} {unit}"
        num /= 1024.0

def recv_line(f) -> Optional[str]:
    line = f.readline()
    if not line:
        return None
    return line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")

def recv_exact(f, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = f.read(n - len(data))
        if not chunk:
            break
        data += chunk
    return data

# ===== Protocol =====
# - UPLOAD name_len size\n
#   [name bytes utf-8 length=name_len]
#   [file data size bytes]
#   Response: "OK\n" or "ERR message\n"
# - LIST\n -> "OK len\n" + [JSON bytes]
# - PING\n -> "PONG\n"

# ===== Server =====
class FileTransferServer:
    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT, upload_dir: str = UPLOAD_DIR,
                 log_cb: Optional[Callable[[str], None]] = None):
        self.host = host
        self.port = port
        self.upload_dir = upload_dir
        self._sock = None
        self._stop_event = threading.Event()
        self._accept_thread = None
        self.log_cb = log_cb
        ensure_dir(self.upload_dir)

    def log(self, msg: str):
        logging.info(msg)
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def start(self):
        if self._accept_thread and self._accept_thread.is_alive():
            return
        self._stop_event.clear()
        self._accept_thread = threading.Thread(target=self._serve, name="ServerAcceptThread", daemon=True)
        self._accept_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._accept_thread:
            self._accept_thread.join(timeout=2.0)
        self.log("[SERVER] Stopped")

    @property
    def running(self) -> bool:
        return self._accept_thread is not None and self._accept_thread.is_alive()

    def _serve(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(LISTEN_BACKLOG)
            self._sock.settimeout(1.0)
            self.log(f"[SERVER] Listening on {self.host}:{self.port} (IP: {get_local_ip()})")
            while not self._stop_event.is_set():
                try:
                    client, addr = self._sock.accept()
                except socket.timeout:
                    continue
                t = threading.Thread(target=self._handle_client, args=(client, addr), daemon=True)
                t.start()
        except Exception as e:
            self.log(f"[SERVER] Error: {e!r}")
        finally:
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass

    def _handle_client(self, client: socket.socket, addr):
        self.log(f"[CONN] {addr} connected")
        with client:
            try:
                f = client.makefile("rb")
                line = recv_line(f)
                if not line:
                    self.log(f"[CONN] {addr} closed without command")
                    return
                if line.startswith("UPLOAD "):
                    parts = line.split()
                    if len(parts) != 3:
                        self._sendall(client, b"ERR invalid_header\n")
                        self.log(f"[UPLOAD] {addr} invalid header: {line}")
                        return
                    try:
                        name_len = int(parts[1])
                        file_size = int(parts[2])
                    except ValueError:
                        self._sendall(client, b"ERR invalid_numbers\n")
                        return
                    if name_len <= 0 or name_len > 4096:
                        self._sendall(client, b"ERR name_len_out_of_range\n")
                        return
                    if file_size < 0 or file_size > MAX_FILE_SIZE:
                        self._sendall(client, b"ERR size_limit\n")
                        return

                    raw_name = recv_exact(f, name_len)
                    if len(raw_name) != name_len:
                        self._sendall(client, b"ERR name_read_incomplete\n")
                        return
                    filename_raw = raw_name.decode("utf-8", errors="replace")
                    filename = sanitize_filename(filename_raw)
                    if not filename:
                        self._sendall(client, b"ERR invalid_filename\n")
                        return

                    final_path = make_unique_path(self.upload_dir, filename)
                    tmp_path = final_path + ".part"
                    self.log(f"[UPLOAD] Receiving '{filename}' -> {final_path} ({human_bytes(file_size)}) from {addr}")

                    written = 0
                    ok = False
                    try:
                        with open(tmp_path, "wb") as out:
                            remaining = file_size
                            while remaining > 0:
                                chunk = f.read(min(CHUNK_SIZE, remaining))
                                if not chunk:
                                    break
                                out.write(chunk)
                                remaining -= len(chunk)
                                written += len(chunk)
                        if written == file_size:
                            os.replace(tmp_path, final_path)
                            ok = True
                        else:
                            self.log(f"[UPLOAD] Incomplete upload from {addr} for '{filename}' ({written}/{file_size})")
                    finally:
                        if not ok:
                            try:
                                if os.path.exists(tmp_path):
                                    os.remove(tmp_path)
                            except Exception:
                                pass

                    if ok:
                        self._sendall(client, b"OK\n")
                        self.log(f"[UPLOAD] Saved: {final_path} ({human_bytes(file_size)})")
                    else:
                        self._sendall(client, b"ERR incomplete\n")

                elif line == "LIST":
                    items = []
                    try:
                        for name in os.listdir(self.upload_dir):
                            full = os.path.join(self.upload_dir, name)
                            if os.path.isfile(full):
                                try:
                                    st = os.stat(full)
                                    items.append({
                                        "name": name,
                                        "size": st.st_size,
                                        "mtime": int(st.st_mtime),
                                    })
                                except Exception:
                                    pass
                    except Exception as e:
                        self._sendall(client, f"ERR {e}\n".encode("utf-8"))
                        return
                    payload = json.dumps({"files": items}, ensure_ascii=False).encode("utf-8")
                    self._sendall(client, f"OK {len(payload)}\n".encode("utf-8"))
                    self._sendall(client, payload)

                elif line == "PING":
                    self._sendall(client, b"PONG\n")

                else:
                    self._sendall(client, b"ERR unknown_command\n")
            except Exception as e:
                self.log(f"[CONN] {addr} error: {e!r}")
        self.log(f"[CONN] {addr} disconnected")

    def _sendall(self, s: socket.socket, data: bytes):
        try:
            s.sendall(data)
        except Exception:
            pass


# ===== Client helpers =====
class ClientProto:
    @staticmethod
    def list_remote(host: str, port: int, timeout: float = 10.0) -> Optional[List[dict]]:
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall(b"LIST\n")
                f = s.makefile("rb")
                header = recv_line(f)
                if not header or not header.startswith("OK "):
                    return None
                length = int(header.split()[1])
                data = recv_exact(f, length)
                obj = json.loads(data.decode("utf-8", errors="replace"))
                return obj.get("files", [])
        except Exception:
            return None

    @staticmethod
    def ping(host: str, port: int, timeout: float = 5.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall(b"PING\n")
                f = s.makefile("rb")
                resp = recv_line(f)
                return resp == "PONG"
        except Exception:
            return False

    @staticmethod
    def upload_file(host: str, port: int, filepath: str,
                    progress_cb: Optional[Callable[[int, int, float], None]] = None,
                    log_cb: Optional[Callable[[str], None]] = None,
                    timeout: float = 30.0) -> Tuple[bool, str]:
        def log(msg: str):
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass

        try:
            filename = os.path.basename(filepath)
            filesize = os.path.getsize(filepath)
        except Exception as e:
            return False, f"Cannot open file: {e}"

        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                # Send header
                name_bytes = filename.encode("utf-8")
                header = f"UPLOAD {len(name_bytes)} {filesize}\n".encode("utf-8")
                s.sendall(header)
                s.sendall(name_bytes)

                sent = 0
                t0 = time.time()
                last_report_t = t0
                last_report_sent = 0

                # Stream file
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        s.sendall(chunk)
                        sent += len(chunk)
                        now = time.time()
                        if progress_cb and (now - last_report_t >= 0.2 or sent == filesize):
                            elapsed = max(1e-3, now - last_report_t)
                            delta = sent - last_report_sent
                            speed = delta / elapsed
                            progress_cb(sent, filesize, speed)
                            last_report_t = now
                            last_report_sent = sent

                # Response
                f_sock = s.makefile("rb")
                resp = recv_line(f_sock)
                if resp == "OK":
                    return True, "OK"
                else:
                    return False, resp or "ERR no_response"
        except Exception as e:
            return False, f"ERROR: {e}"


# ===== Auto-start (Windows) =====
class AutoStartManager:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE_NAME = APP_NAME

    @staticmethod
    def build_command(start_hidden: bool = True, start_server: bool = True) -> str:
        if getattr(sys, 'frozen', False):
            # PyInstaller exe
            cmd = f'"{sys.executable}"'
        else:
            script = os.path.abspath(sys.argv[0])
            py_exec = sys.executable
            if os.name == 'nt':
                pyw = os.path.join(os.path.dirname(py_exec), 'pythonw.exe')
                if os.path.exists(pyw):
                    py_exec = pyw
            cmd = f'"{py_exec}" "{script}"'
        if start_hidden:
            cmd += " --background"
        if start_server:
            cmd += " --start-server"
        return cmd

    @staticmethod
    def enable(command: Optional[str] = None) -> Tuple[bool, str]:
        if os.name != 'nt':
            return False, "Windows only"
        try:
            import winreg  # type: ignore
            if command is None:
                command = AutoStartManager.build_command()
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AutoStartManager.RUN_KEY, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, AutoStartManager.VALUE_NAME, 0, winreg.REG_SZ, command)
            winreg.CloseKey(key)
            return True, "Enabled"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def disable() -> Tuple[bool, str]:
        if os.name != 'nt':
            return False, "Windows only"
        try:
            import winreg  # type: ignore
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AutoStartManager.RUN_KEY, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.DeleteValue(key, AutoStartManager.VALUE_NAME)
            finally:
                winreg.CloseKey(key)
            return True, "Disabled"
        except FileNotFoundError:
            return True, "Not present"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def is_enabled() -> Tuple[bool, Optional[str]]:
        if os.name != 'nt':
            return False, None
        try:
            import winreg  # type: ignore
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AutoStartManager.RUN_KEY, 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, AutoStartManager.VALUE_NAME)
            winreg.CloseKey(key)
            return True, val
        except FileNotFoundError:
            return False, None
        except Exception:
            return False, None


# ===== Config =====
def load_config() -> dict:
    cfg = {
        "port": DEFAULT_PORT,
        "upload_dir": UPLOAD_DIR,
        "minimize_to_tray": True,
        "start_hidden": False,
        "start_server_on_launch": False,
        "host_history": ["localhost", "127.0.0.1"],  # Thêm lịch sử host
        # "windows_startup": not stored; we read registry directly
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            cfg.update(old)
            # Đảm bảo host_history luôn có ít nhất các giá trị mặc định
            if "host_history" not in cfg:
                cfg["host_history"] = ["localhost", "127.0.0.1"]
            # Đảm bảo upload_dir luôn sử dụng đường dẫn tuyệt đối
            if "upload_dir" in old and not os.path.isabs(old["upload_dir"]):
                cfg["upload_dir"] = UPLOAD_DIR
        except Exception:
            pass
    return cfg

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ===== GUI =====
class UiDispatcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self._pump()

    def post(self, fn: Callable, *args, **kwargs):
        self.q.put((fn, args, kwargs))

    def _pump(self):
        try:
            while True:
                fn, args, kwargs = self.q.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

class TransferRow:
    def __init__(self, parent, filename: str, size: int):
        self.frame = ttk.Frame(parent)
        self.filename = filename
        self.size = size

        self.label = ttk.Label(self.frame, text=filename, width=40)
        self.label.grid(row=0, column=0, sticky="w", padx=4)

        self.size_lbl = ttk.Label(self.frame, text=human_bytes(size), width=10)
        self.size_lbl.grid(row=0, column=1, sticky="e")

        self.progress = ttk.Progressbar(self.frame, orient="horizontal", length=300, mode="determinate", maximum=1000)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 0))

        self.info_lbl = ttk.Label(self.frame, text="Queued...", foreground="#555")
        self.info_lbl.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(2, 6))

        self.frame.columnconfigure(0, weight=1)

    def set_status(self, text: str, color: Optional[str] = None):
        self.info_lbl.config(text=text)
        if color:
            self.info_lbl.config(foreground=color)

    def set_progress(self, sent: int, total: int, speed: float):
        pct = 0 if total <= 0 else int(sent / total * 1000)
        self.progress['value'] = pct
        speed_txt = human_bytes(speed) + "/s" if speed > 0 else ""
        if sent < total:
            eta_txt = ""
            if speed > 0:
                remain = total - sent
                eta = remain / speed
                if eta >= 1:
                    eta_txt = f" | ETA {int(eta)}s"
            self.info_lbl.config(text=f"{human_bytes(sent)}/{human_bytes(total)} | {speed_txt}{eta_txt}", foreground="#333")
        else:
            self.info_lbl.config(text=f"Done | {human_bytes(total)}", foreground="green")

class AppGUI:
    def __init__(self, root, start_hidden_flag=False, start_server_flag=False):
        self.root = root
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.dispatch = UiDispatcher(root)

        # Server + config
        self.cfg = load_config()
        self.server = FileTransferServer(
            host="0.0.0.0",
            port=int(self.cfg.get("port", DEFAULT_PORT)),
            upload_dir=self.cfg.get("upload_dir", UPLOAD_DIR),
            log_cb=self._log_server,
        )

        # Host/port for client
        self.host_history = self.cfg.get("host_history", ["localhost", "127.0.0.1"])
        if not self.host_history:  # Đảm bảo luôn có ít nhất một giá trị
            self.host_history = ["localhost", "127.0.0.1"]
        self.client_host_var = tk.StringVar(value=self.host_history[0])
        self.client_port_var = tk.IntVar(value=int(self.cfg.get("port", DEFAULT_PORT)))

        # Tray
        self.tray_icon = None
        self._tray_notified = False

        # Build UI
        self._build_ui()

        # Tray setup
        self._setup_tray()

        # Startup actions
        if start_server_flag or self.cfg.get("start_server_on_launch", False):
            self._start_server()

        if start_hidden_flag or self.cfg.get("start_hidden", False):
            # Hide shortly after tray is ready
            self.root.after(200, self._hide_to_tray)

    def _build_ui(self):
        # Styles (optional theme)
        try:
            self.root.tk.call("source", "azure.tcl")
            ttk.Style().theme_use("azure")
        except Exception:
            pass

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Server tab
        server_tab = ttk.Frame(notebook)
        notebook.add(server_tab, text="Server")

        # Client tab
        client_tab = ttk.Frame(notebook)
        notebook.add(client_tab, text="Client")

        # Settings tab
        settings_tab = ttk.Frame(notebook)
        notebook.add(settings_tab, text="Settings")

        # ----- Server tab -----
        top = ttk.Frame(server_tab)
        top.pack(fill="x")

        self.server_status_lbl = ttk.Label(top, text="[STOPPED]", foreground="red")
        self.server_status_lbl.pack(side="left", padx=4)

        ttk.Label(top, text="Port:").pack(side="left", padx=(10, 2))
        self.port_var = tk.IntVar(value=int(self.cfg.get("port", DEFAULT_PORT)))
        self.port_entry = ttk.Entry(top, textvariable=self.port_var, width=8)
        self.port_entry.pack(side="left")

        self.btn_apply_port = ttk.Button(top, text="Apply", command=self._apply_port)
        self.btn_apply_port.pack(side="left", padx=5)

        self.btn_start = ttk.Button(top, text="Start Server", command=self._start_server)
        self.btn_start.pack(side="left", padx=5)

        self.btn_stop = ttk.Button(top, text="Stop", command=self._stop_server, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

        # Thêm nút Reset IP
        self.btn_reset_ip = ttk.Button(top, text="Reset IP", command=self._reset_ip)
        self.btn_reset_ip.pack(side="left", padx=5)

        self.ip_lbl = ttk.Label(top, text=f"IP: {get_local_ip()}")
        self.ip_lbl.pack(side="right", padx=4)

        # File list
        mid = ttk.LabelFrame(server_tab, text="Server files")
        mid.pack(fill="both", expand=True, pady=8)

        columns = ("name", "size", "mtime")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings")
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("mtime", text="Modified")
        self.tree.column("name", width=300, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("mtime", width=160, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        btns = ttk.Frame(server_tab)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Refresh", command=self._refresh_local_files).pack(side="left", padx=4)
        ttk.Button(btns, text="Open Folder", command=self._open_upload_folder).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear Log", command=self._clear_server_log).pack(side="left", padx=4)

        # Server log
        log_frame = ttk.LabelFrame(server_tab, text="Server log")
        log_frame.pack(fill="both", expand=True)
        self.server_log = tk.Text(log_frame, height=10)
        self.server_log.pack(fill="both", expand=True)

        # ----- Client tab -----
        c_top = ttk.LabelFrame(client_tab, text="Destination")
        c_top.pack(fill="x", pady=6)

        ttk.Label(c_top, text="Host:").pack(side="left", padx=(6, 2))
        # Thay đổi Entry thành Combobox với lịch sử
        self.host_combo = ttk.Combobox(c_top, textvariable=self.client_host_var, width=16, values=self.host_history)
        self.host_combo.pack(side="left")
        # Bind sự kiện khi chọn host mới
        self.host_combo.bind("<<ComboboxSelected>>", self._on_host_selected)
        # Bind sự kiện khi người dùng nhập text mới
        self.host_combo.bind("<KeyRelease>", self._on_host_key_release)
        ttk.Label(c_top, text="Port:").pack(side="left", padx=(10, 2))
        ttk.Entry(c_top, textvariable=self.client_port_var, width=8).pack(side="left")
        ttk.Button(c_top, text="Ping", command=self._ping_server).pack(side="left", padx=6)
        ttk.Button(c_top, text="List Remote Files", command=self._list_remote).pack(side="left", padx=6)

        c_mid = ttk.LabelFrame(client_tab, text="Send files")
        c_mid.pack(fill="x", pady=6)
        ttk.Button(c_mid, text="Select Files...", command=self._select_files).pack(side="left", padx=6, pady=6)

        # Drag & Drop area
        self.drop_area = None
        if DND_AVAILABLE and isinstance(self.root, TkinterDnD.Tk):
            self.drop_area = tk.Label(c_mid, text="Drag & Drop files here", relief="sunken", height=8)  # Tăng height từ 6 lên 8
            self.drop_area.pack(fill="x", padx=6, pady=6)
            self.drop_area.drop_target_register(DND_FILES)
            self.drop_area.dnd_bind("<<Drop>>", self._on_drop_files)
        else:
            tip = "Install 'tkinterdnd2' to enable Drag & Drop: pip install tkinterdnd2"
            self.drop_area = ttk.Label(c_mid, text=tip, foreground="#777")
            self.drop_area.pack(fill="x", padx=6, pady=6)

        # Transfers list - thu gọn chiều cao
        t_frame = ttk.LabelFrame(client_tab, text="Transfers")
        t_frame.pack(fill="both", expand=True)

        self.transfers_canvas = tk.Canvas(t_frame, borderwidth=0, highlightthickness=0, height=100)  # Thu gọn chiều cao từ 120 xuống 100
        self.transfers_frame = ttk.Frame(self.transfers_canvas)
        self.transfers_scroll = ttk.Scrollbar(t_frame, orient="vertical", command=self.transfers_canvas.yview)
        self.transfers_canvas.configure(yscrollcommand=self.transfers_scroll.set)

        self.transfers_canvas.pack(side="left", fill="both", expand=True)
        self.transfers_scroll.pack(side="right", fill="y")
        self._transfers_window = self.transfers_canvas.create_window((0, 0), window=self.transfers_frame, anchor="nw")

        def on_configure(event):
            self.transfers_canvas.configure(scrollregion=self.transfers_canvas.bbox("all"))
            self.transfers_canvas.itemconfig(self._transfers_window, width=event.width)
        self.transfers_canvas.bind("<Configure>", on_configure)
        self.transfers_frame.bind("<Configure>", lambda e: self.transfers_canvas.configure(scrollregion=self.transfers_canvas.bbox("all")))

        # Client log
        c_log = ttk.LabelFrame(client_tab, text="Client log")
        c_log.pack(fill="both", expand=True)
        self.client_log = tk.Text(c_log, height=8)
        self.client_log.pack(fill="both", expand=True)

        # ----- Settings tab -----
        s_general = ttk.LabelFrame(settings_tab, text="General")
        s_general.pack(fill="x", padx=8, pady=8)

        self.var_minimize_to_tray = tk.BooleanVar(value=bool(self.cfg.get("minimize_to_tray", True)))
        ttk.Checkbutton(s_general, text="Minimize to system tray when closing window", variable=self.var_minimize_to_tray,
                        command=self._save_settings).pack(anchor="w", padx=8, pady=4)

        self.var_start_hidden = tk.BooleanVar(value=bool(self.cfg.get("start_hidden", False)))
        ttk.Checkbutton(s_general, text="Start hidden in system tray", variable=self.var_start_hidden,
                        command=self._save_settings).pack(anchor="w", padx=8, pady=4)

        self.var_start_server_on_launch = tk.BooleanVar(value=bool(self.cfg.get("start_server_on_launch", False)))
        ttk.Checkbutton(s_general, text="Start server automatically on app launch", variable=self.var_start_server_on_launch,
                        command=self._save_settings).pack(anchor="w", padx=8, pady=4)

        s_windows = ttk.LabelFrame(settings_tab, text="Windows Startup")
        s_windows.pack(fill="x", padx=8, pady=8)
        if os.name == 'nt':
            enabled, command = AutoStartManager.is_enabled()
            self.var_windows_startup = tk.BooleanVar(value=enabled)
            self.lbl_win_cmd = ttk.Label(s_windows, text=f"Command: {command or '(none)'}", foreground="#555")
            ttk.Checkbutton(s_windows, text="Run FileTransferOne when I sign in (Windows)", variable=self.var_windows_startup,
                            command=self._apply_windows_startup).pack(anchor="w", padx=8, pady=4)
            self.lbl_win_cmd.pack(fill="x", padx=8, pady=(0, 6))
            ttk.Button(s_windows, text="Apply now", command=self._apply_windows_startup).pack(anchor="w", padx=8, pady=4)
        else:
            ttk.Label(s_windows, text="Not available on this OS").pack(anchor="w", padx=8, pady=4)

        # Initial
        self._update_server_status(False)
        self._refresh_local_files()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ===== Tray =====
    def _setup_tray(self):
        if not TRAY_AVAILABLE:
            self._log_client("Tray not available. Install: pip install pystray pillow")
            return
        try:
            image = self._make_tray_image()
            self.tray_icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} v{VERSION}", self._build_tray_menu())
            t = threading.Thread(target=self.tray_icon.run, daemon=True)
            t.start()
        except Exception as e:
            self._log_client(f"[TRAY] Failed to init tray: {e}")

    def _build_tray_menu(self):
        if not TRAY_AVAILABLE:
            return None
        return pystray.Menu(
            pystray.MenuItem(lambda item: "Show Window" if not self._is_window_visible() else "Hide Window",
                             self._on_tray_toggle_window),
            pystray.MenuItem(lambda item: "Stop Server" if self.server.running else "Start Server",
                             self._on_tray_toggle_server),
            pystray.MenuItem("Open Upload Folder", self._on_tray_open_folder),
            pystray.MenuItem("Quit", self._on_tray_quit)
        )

    def _refresh_tray_menu(self):
        if TRAY_AVAILABLE and self.tray_icon:
            try:
                self.tray_icon.menu = self._build_tray_menu()
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _make_tray_image(self):
        size = 64
        img = Image.new("RGBA", (size, size), (30, 144, 255, 255))  # DodgerBlue
        d = ImageDraw.Draw(img)
        # Upload-like arrow + base
        d.polygon([(32, 10), (20, 26), (26, 26), (26, 44), (38, 44), (38, 26), (44, 26)], fill="white")
        d.rectangle([(20, 46), (44, 52)], fill="white")
        return img

    def _is_window_visible(self) -> bool:
        try:
            return self.root.state() != 'withdrawn'
        except Exception:
            return True

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.after(50, self.root.lift)
        except Exception:
            pass
        self._refresh_tray_menu()

    def _hide_to_tray(self):
        if TRAY_AVAILABLE and self.tray_icon:
            self.root.withdraw()
            if not self._tray_notified:
                self._tray_notified = True
                self._log_client("Running in system tray. Right-click tray icon for actions.")
        else:
            self.root.iconify()
        self._refresh_tray_menu()

    # Tray callbacks (called from tray thread) -> dispatch to UI thread
    def _on_tray_toggle_window(self, icon=None, item=None):
        if self._is_window_visible():
            self.dispatch.post(self._hide_to_tray)
        else:
            self.dispatch.post(self._show_window)

    def _on_tray_toggle_server(self, icon=None, item=None):
        if self.server.running:
            self.dispatch.post(self._stop_server)
        else:
            self.dispatch.post(self._start_server)
        self.dispatch.post(self._refresh_tray_menu)

    def _on_tray_open_folder(self, icon=None, item=None):
        self.dispatch.post(self._open_upload_folder)

    def _on_tray_quit(self, icon, item):
        try:
            icon.visible = False
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass
        self.dispatch.post(self._really_quit)

    def _really_quit(self):
        try:
            if self.server.running:
                self.server.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)

    # ===== Server UI =====
    def _apply_port(self):
        try:
            new_port = int(self.port_var.get())
            if new_port < 1 or new_port > 65535:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid port", "Port must be 1..65535")
            return
        self.cfg["port"] = new_port
        save_config(self.cfg)
        self.server.port = new_port
        self.client_port_var.set(new_port)
        self._log_server(f"[CONFIG] Port set to {new_port}. Restart server to apply.")
        if self.server.running:
            self._stop_server()
            self._start_server()

    def _start_server(self):
        try:
            self.server.start()
            self._update_server_status(True)
            self._log_server("[SERVER] Started")
            self._refresh_tray_menu()
        except Exception as e:
            messagebox.showerror("Server error", str(e))

    def _stop_server(self):
        try:
            self.server.stop()
            self._update_server_status(False)
            self._log_server("[SERVER] Stopped")
            self._refresh_tray_menu()
        except Exception as e:
            messagebox.showerror("Server error", str(e))

    def _update_server_status(self, running: bool):
        if running and self.server.running:
            self.server_status_lbl.config(text="[RUNNING]", foreground="green")
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.server_status_lbl.config(text="[STOPPED]", foreground="red")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

    def _refresh_local_files(self):
        try:
            ensure_dir(self.server.upload_dir)
            for i in self.tree.get_children():
                self.tree.delete(i)
            rows = []
            for name in os.listdir(self.server.upload_dir):
                full = os.path.join(self.server.upload_dir, name)
                if os.path.isfile(full):
                    st = os.stat(full)
                    rows.append((name, st.st_size, int(st.st_mtime)))
            rows.sort(key=lambda x: x[2], reverse=True)
            for name, size, mtime in rows:
                self.tree.insert("", "end", values=(
                    name, human_bytes(size), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                ))
        except Exception as e:
            self._log_server(f"[ERROR] Refresh files: {e}")

    def _open_upload_folder(self):
        path = os.path.abspath(self.server.upload_dir)
        ensure_dir(path)
        try:
            if platform.system() == "Windows":
                os.startfile(path)  # type: ignore
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Open folder error", str(e))

    def _clear_server_log(self):
        try:
            self.server_log.delete("1.0", "end")
        except Exception:
            pass

    def _log_server(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        def append():
            self.server_log.insert("end", f"[{ts}] {msg}\n")
            self.server_log.see("end")
        self.dispatch.post(append)

    # ===== Client actions =====
    def _log_client(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        def append():
            self.client_log.insert("end", f"[{ts}] {msg}\n")
            self.client_log.see("end")
        self.dispatch.post(append)

    def _on_close(self):
        if self.cfg.get("minimize_to_tray", True):
            self._hide_to_tray()
        else:
            self._really_quit()

    def _ping_server(self):
        host = self.client_host_var.get().strip()
        port = int(self.client_port_var.get())
        # Tự động thêm host vào lịch sử
        self._add_host_to_history(host)
        def worker():
            ok = ClientProto.ping(host, port)
            if ok:
                self._log_client(f"[PING] {host}:{port} -> PONG")
            else:
                self._log_client(f"[PING] {host}:{port} -> FAILED")
        threading.Thread(target=worker, daemon=True).start()

    def _list_remote(self):
        host = self.client_host_var.get().strip()
        port = int(self.client_port_var.get())
        # Tự động thêm host vào lịch sử
        self._add_host_to_history(host)
        def worker():
            files = ClientProto.list_remote(host, port)
            if files is None:
                self._log_client(f"[LIST] {host}:{port} -> ERROR")
                return
            if not files:
                self._log_client(f"[LIST] {host}:{port} -> no files")
                return
            self._log_client(f"[LIST] {host}:{port} -> {len(files)} files:")
            for it in files:
                name = it.get("name", "?")
                size = it.get("size", 0)
                mtime = it.get("mtime", 0)
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else "?"
                self._log_client(f" - {name} | {human_bytes(size)} | {when}")
        threading.Thread(target=worker, daemon=True).start()

    def _select_files(self):
        paths = filedialog.askopenfilenames()
        if not paths:
            return
        for p in paths:
            if os.path.isfile(p):
                self._start_upload(p)

    def _on_drop_files(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for p in paths:
            p = p.strip()
            if os.path.isfile(p):
                self._start_upload(p)

    def _add_transfer_row(self, filepath: str) -> TransferRow:
        try:
            size = os.path.getsize(filepath)
        except Exception:
            size = 0
        row = TransferRow(self.transfers_frame, os.path.basename(filepath), size)
        row.frame.pack(fill="x", padx=6, pady=4)
        return row

    def _start_upload(self, filepath: str):
        host = self.client_host_var.get().strip()
        port = int(self.client_port_var.get())
        # Tự động thêm host vào lịch sử
        self._add_host_to_history(host)
        row = self._add_transfer_row(filepath)

        def worker():
            def progress(sent, total, speed):
                self.dispatch.post(row.set_progress, sent, total, speed)
            def log_cb(msg):
                self._log_client(msg)
            self.dispatch.post(row.set_status, "Uploading...", "#333")
            ok, msg = ClientProto.upload_file(host, port, filepath, progress_cb=progress, log_cb=log_cb)
            def finalize():
                if ok:
                    row.set_status("Completed", "green")
                    row.set_progress(row.size, row.size, 0.0)
                    if self.server.running and port == self.server.port and host in ("localhost", "127.0.0.1", get_local_ip()):
                        self._refresh_local_files()
                else:
                    row.set_status(msg, "red")
                self._log_client(f"[UPLOAD] {os.path.basename(filepath)} -> {msg}")
            self.dispatch.post(finalize)
        threading.Thread(target=worker, daemon=True).start()

    # ===== Settings handlers =====
    def _save_settings(self):
        self.cfg["minimize_to_tray"] = bool(self.var_minimize_to_tray.get())
        self.cfg["start_hidden"] = bool(self.var_start_hidden.get())
        self.cfg["start_server_on_launch"] = bool(self.var_start_server_on_launch.get())
        save_config(self.cfg)
        self._log_client("[CONFIG] Settings saved")

    def _apply_windows_startup(self):
        if os.name != 'nt':
            return
        want = bool(getattr(self, 'var_windows_startup', tk.BooleanVar(value=False)).get())
        if want:
            cmd = AutoStartManager.build_command(
                start_hidden=bool(self.var_start_hidden.get()),
                start_server=bool(self.var_start_server_on_launch.get())
            )
            ok, msg = AutoStartManager.enable(cmd)
            self._log_client(f"[STARTUP] Enable -> {msg}")
        else:
            ok, msg = AutoStartManager.disable()
            self._log_client(f"[STARTUP] Disable -> {msg}")
        enabled, command = AutoStartManager.is_enabled()
        if hasattr(self, 'var_windows_startup'):
            self.var_windows_startup.set(enabled)
        if hasattr(self, 'lbl_win_cmd'):
            self.lbl_win_cmd.config(text=f"Command: {command or '(none)'}")

    def _reset_ip(self):
        new_ip = get_local_ip()
        self.ip_lbl.config(text=f"IP: {new_ip}")
        # Cập nhật lịch sử host với IP mới
        if self._add_host_to_history(new_ip):
            self._log_server(f"[CONFIG] IP reset to {new_ip} and added to host history")
        else:
            self._log_server(f"[CONFIG] IP reset to {new_ip}")

    def _on_host_selected(self, event):
        # Cập nhật lịch sử host khi người dùng chọn hoặc nhập host mới
        current_host = self.client_host_var.get().strip()
        if current_host:
            self._add_host_to_history(current_host)

    def _on_host_key_release(self, event):
        # Cập nhật lịch sử host khi người dùng nhập text mới vào combobox
        # Chỉ cập nhật khi người dùng nhấn Enter hoặc Tab
        if event.keysym in ('Return', 'Tab'):
            current_host = self.client_host_var.get().strip()
            if current_host:
                if self._add_host_to_history(current_host):
                    self._log_client(f"[CONFIG] Added host to history: {current_host}")

    def _add_host_to_history(self, host: str):
        """Thêm host vào lịch sử nếu chưa có"""
        if host and host not in self.cfg.get("host_history", []):
            if "host_history" not in self.cfg:
                self.cfg["host_history"] = []
            self.cfg["host_history"].insert(0, host)
            # Giữ tối đa 10 host trong lịch sử
            self.cfg["host_history"] = self.cfg["host_history"][:10]
            # Cập nhật biến instance
            self.host_history = self.cfg["host_history"]
            # Cập nhật combobox values
            if hasattr(self, 'host_combo'):
                self.host_combo['values'] = self.host_history
            save_config(self.cfg)
            return True  # Trả về True nếu đã thêm host mới
        return False  # Trả về False nếu host đã tồn tại

# ===== Entrypoint =====
def run_gui(background=False, start_server=False):
    # Use TkinterDnD.Tk if available for DnD
    if DND_AVAILABLE:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()
    root.minsize(800, 600)
    app = AppGUI(root, start_hidden_flag=background, start_server_flag=start_server)
    root.mainloop()

def run_server_only():
    cfg = load_config()
    port = int(cfg.get("port", DEFAULT_PORT))
    upload_dir = cfg.get("upload_dir", UPLOAD_DIR)
    server = FileTransferServer(host="0.0.0.0", port=port, upload_dir=upload_dir)
    server.start()
    print(f"{APP_NAME} server running on 0.0.0.0:{port} (IP: {get_local_ip()}) - press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping server...")
    finally:
        server.stop()

def main():
    args = set(a.lower() for a in sys.argv[1:])
    if "server" in args or "--server" in args:
        run_server_only()
        return
    background = ("--background" in args) or ("-bg" in args)
    start_server = ("--start-server" in args) or ("-ss" in args)
    run_gui(background=background, start_server=start_server)

if __name__ == "__main__":
    main()