"""Секция 'APP TERMINAL' монолита `app/server_backup.py`.

Вынесена механически (`tools/_extract_section.py`): код ниже не правился,
имена из других секций монолита не импортируются, а связываются
хвостом монолита через `_bind()` (см. `_FOREIGN`): порядок хвостовых
ре-экспортов произвольный, и прямой импорт упёрся бы в частично
инициализированный модуль.
Монолит ре-экспортирует всё, что определено здесь, поэтому прежние
потребители (`import server`, `import server_backup`) работают без правок.
"""

# перенесено из головы монолита: эти имена секция использовала как импорты
import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)
import ask_modes
import swallow

# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'os',
    'subprocess',
    'threading',
)

def _bind(namespace=None):
    if namespace is None:
        import server_backup as _sb
        namespace = vars(_sb)
    _g = globals()
    for _n in _FOREIGN:
        if _n not in _g and _n in namespace:
            _g[_n] = namespace[_n]
    return [n for n in _FOREIGN if n not in globals()]
# === APP TERMINAL ===
_app_terminals = {}  # {app_id: {"process": proc, "output": [], "input_queue": []}}
_app_terminal_lock = threading.Lock()


def _start_app_terminal_windows(app_id, app_dir, cmd):
    """Запуск терминала приложения на Windows.

    PTY на Windows нет: pty/termios/fcntl — POSIX-модули, их импорт падал
    с ModuleNotFoundError («No module named 'termios'»), и терминал
    приложений на Windows не работал вовсе. Полноценной эмуляции консоли
    без pywinpty здесь не получится, но обычные пайпы дают рабочий поток
    вывода процесса, а CREATE_NO_WINDOW не даёт мигать окном.
    """
    cmd_list = cmd.split() if isinstance(cmd, str) else list(cmd)
    if "python" in cmd_list[0].lower() and "-u" not in cmd_list:
        cmd_list.insert(1, "-u")
    proc = subprocess.Popen(
        cmd_list,
        cwd=app_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
    )
    output_buffer = []

    def reader():
        try:
            for line in proc.stdout:
                output_buffer.append(line)
        except Exception:
            swallow.note('terminal.reader:L82')
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    _app_terminals[app_id] = {
        "process": proc,
        "master_fd": None,
        "output_buffer": output_buffer,
        "reader_thread": t,
        "running": True,
        "use_pty": False,
    }
    return {"ok": True}


def start_app_terminal(app_id, app_dir, cmd):
    """Start a Python process for terminal display with full PTY interactive support."""
    global _app_terminals
    with _app_terminal_lock:
        if app_id in _app_terminals:
            return {"error": "Already running"}
        # На Windows PTY недоступен — отдельная ветка на обычных пайпах.
        if os.name == "nt":
            try:
                return _start_app_terminal_windows(app_id, app_dir, cmd)
            except Exception as e:
                import traceback
                traceback.print_exc()
                return {"error": str(e)}
        try:
            import pty, select as select_mod, termios

            cmd_list = cmd.split() if isinstance(cmd, str) else cmd

            # Create PTY for proper terminal emulation
            master_fd, slave_fd = pty.openpty()

            # Set terminal size
            try:
                import fcntl, struct
                winsize = struct.pack('HHHH', 24, 80, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                swallow.note('terminal.start_app_terminal:L126')
                pass

            # For Python: ensure -u for unbuffered output
            if 'python' in cmd_list[0].lower():
                if '-u' not in cmd_list:
                    cmd_list.insert(1, '-u')
            
            proc = subprocess.Popen(
                cmd_list,
                cwd=app_dir,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid  # New process group
            )
            os.close(slave_fd)
            
            output_buffer = []
            
            def read_pty():
                """Read from PTY master and store output."""
                try:
                    while True:
                        r, _, _ = select_mod.select([master_fd], [], [], 0.1)
                        if r:
                            try:
                                data = os.read(master_fd, 4096)
                                if not data:
                                    break
                                output_buffer.append(data.decode('utf-8', errors='replace'))
                            except OSError:
                                swallow.note('terminal.read_pty:L159')
                                break
                        # Check if process is still running
                        if proc.poll() is not None:
                            # Read remaining data
                            try:
                                data = os.read(master_fd, 4096)
                                if data:
                                    output_buffer.append(data.decode('utf-8', errors='replace'))
                            except Exception:
                                swallow.note('terminal.read_pty:L169')
                                pass
                            break
                except Exception:
                    swallow.note('terminal.read_pty:L173')
                    pass
                finally:
                    try:
                        os.close(master_fd)
                    except Exception:
                        swallow.note('terminal.read_pty:L179')
                        pass
            
            reader_thread = threading.Thread(target=read_pty, daemon=True)
            reader_thread.start()
            
            _app_terminals[app_id] = {
                "process": proc,
                "master_fd": master_fd,
                "output_buffer": output_buffer,
                "reader_thread": reader_thread,
                "running": True,
                "use_pty": True
            }
            return {"ok": True}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

def get_app_terminal_output(app_id):
    """Get terminal output for an app."""
    global _app_terminals
    with _app_terminal_lock:
        if app_id not in _app_terminals:
            return {"output": "", "running": False}
        term = _app_terminals[app_id]
        output_buffer = term["output_buffer"]
        running = term.get("running", True)
        full_output = "".join(output_buffer)
        return {"output": full_output, "running": running}


def send_app_terminal_input(app_id, text):
    """Send input to terminal."""
    global _app_terminals
    with _app_terminal_lock:
        if app_id in _app_terminals:
            term = _app_terminals[app_id]
            if term.get("use_pty"):
                master_fd = term.get("master_fd")
                if master_fd is not None:
                    try:
                        os.write(master_fd, text.encode('utf-8'))
                        return {"ok": True}
                    except Exception:
                        swallow.note('terminal.send_app_terminal_input:L225')
                        pass
            else:
                proc = term["process"]
                if proc.stdin:
                    try:
                        proc.stdin.write(text)
                        proc.stdin.flush()
                        return {"ok": True}
                    except Exception:
                        swallow.note('terminal.send_app_terminal_input:L235')
                        pass
    return {"error": "Not running"}


def stop_app_terminal(app_id):
    """Stop terminal process."""
    global _app_terminals
    with _app_terminal_lock:
        if app_id in _app_terminals:
            term = _app_terminals[app_id]
            try:
                term["process"].terminate()
                term["process"].wait(timeout=3)
            except Exception:
                swallow.note('terminal.stop_app_terminal:L250')
                try: term["process"].kill()
                except Exception:
                    swallow.note('terminal.stop_app_terminal:L253')
                    pass
            if term.get("use_pty"):
                try: os.close(term.get("master_fd", -1))
                except Exception:
                    swallow.note('terminal.stop_app_terminal:L258')
                    pass
            del _app_terminals[app_id]
            return {"ok": True}
    return {"error": "Not running"}


