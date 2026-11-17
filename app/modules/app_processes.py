"""Запуск приложений: терминал (pty на posix, pipe на Windows) и
локальный сервер приложения, плюс чтение вывода и остановка.

Перенесено из server_backup.py без изменения тел. Реестры процессов
(_app_terminals, _app_servers) и их блокировки читаются и пишутся через _sb:
живой слой, планировщик и старый код монолита работают с теми же словарями, и
своя копия означала бы, что stop_app_server не видит процесс, запущенный
start_app_server. APPS_DIRS также из _sb: apply_profile_globals переписывает его
при смене профиля.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib

import server_backup as _sb
import swallow

def start_app_terminal(app_id, app_dir, cmd):
    """Start a Python process for terminal display with full PTY interactive support."""
    global _app_terminals
    with _sb._app_terminal_lock:
        if app_id in _sb._app_terminals:
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
                swallow.note('app_processes.start_app_terminal:L50')
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
                                swallow.note('app_processes.read_pty:L83')
                                break
                        # Check if process is still running
                        if proc.poll() is not None:
                            # Read remaining data
                            try:
                                data = os.read(master_fd, 4096)
                                if data:
                                    output_buffer.append(data.decode('utf-8', errors='replace'))
                            except Exception:
                                swallow.note('app_processes.read_pty:L93')
                                pass
                            break
                except Exception:
                    swallow.note('app_processes.read_pty:L97')
                    pass
                finally:
                    try:
                        os.close(master_fd)
                    except Exception:
                        swallow.note('app_processes.read_pty:L103')
                        pass
            
            reader_thread = threading.Thread(target=read_pty, daemon=True)
            reader_thread.start()
            
            _sb._app_terminals[app_id] = {
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

def start_app_server(app_id):
    """Start a local server for an app that needs it."""
    global _app_servers
    
    # Find app directory and read manifest
    app_dir = None
    for d in _sb.APPS_DIRS:
        test_dir = os.path.join(d, app_id)
        if os.path.isdir(test_dir):
            app_dir = test_dir
            break
    
    if not app_dir:
        return {"error": "Приложение не найдено"}
    
    # Read manifest
    manifest_path = os.path.join(app_dir, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            swallow.note('app_processes.start_app_server:L146')
            pass
    
    if not manifest.get("needsServer"):
        return {"error": "Приложение не требует сервера"}
    
    port = manifest.get("serverPort", 8000)
    idle_timeout = manifest.get("idleTimeout", 10)
    cmd = manifest.get("serverCmd", "")
    
    # Check if server is already running
    with _sb._app_server_lock:
        if app_id in _sb._app_servers:
            _sb._app_servers[app_id]["last_activity"] = time.time()
            return {"ok": True, "url": f"http://localhost:{port}", "port": port}
    
    # Start server
    try:
        if cmd:
            # Custom command
            proc = subprocess.Popen(
                cmd.split(),
                cwd=app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        else:
            # Default: Python HTTP server
            proc = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port)],
                cwd=app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        
        with _sb._app_server_lock:
            _sb._app_servers[app_id] = {
                "process": proc,
                "port": port,
                "last_activity": time.time(),
                "timeout": idle_timeout * 60  # Convert to seconds
            }
        
        # Wait for the server to start listening (up to 15 seconds)
        server_url = f"http://127.0.0.1:{port}"
        for attempt in range(30):
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(server_url, timeout=0.5)
                _sb.logger.info(f"App server started for {app_id} on port {port}")
                return {"ok": True, "url": f"http://localhost:{port}", "port": port}
            except Exception:
                swallow.note('app_processes.start_app_server:L199')
                time.sleep(0.5)
        
        # Server didn't respond in time or process exited
        if proc.poll() is not None:
            _sb.logger.warning(f"App server for {app_id} exited prematurely (port {port})")
            with _sb._app_server_lock:
                _sb._app_servers.pop(app_id, None)
            return {"error": f"Server exited before becoming ready"}
        else:
            _sb.logger.info(f"App server started for {app_id} on port {port} (best-effort)")
            return {"ok": True, "url": f"http://localhost:{port}", "port": port}
    except Exception as e:
        _sb.logger.error(f"Failed to start app server for {app_id}: {e}")
        return {"error": str(e)}

def send_app_terminal_input(app_id, text):
    """Send input to terminal."""
    global _app_terminals
    with _sb._app_terminal_lock:
        if app_id in _sb._app_terminals:
            term = _sb._app_terminals[app_id]
            if term.get("use_pty"):
                master_fd = term.get("master_fd")
                if master_fd is not None:
                    try:
                        os.write(master_fd, text.encode('utf-8'))
                        return {"ok": True}
                    except Exception:
                        swallow.note('app_processes.send_app_terminal_input:L228')
                        pass
            else:
                proc = term["process"]
                if proc.stdin:
                    try:
                        proc.stdin.write(text)
                        proc.stdin.flush()
                        return {"ok": True}
                    except Exception:
                        swallow.note('app_processes.send_app_terminal_input:L238')
                        pass
    return {"error": "Not running"}

def stop_app_terminal(app_id):
    """Stop terminal process."""
    global _app_terminals
    with _sb._app_terminal_lock:
        if app_id in _sb._app_terminals:
            term = _sb._app_terminals[app_id]
            try:
                term["process"].terminate()
                term["process"].wait(timeout=3)
            except Exception:
                swallow.note('app_processes.stop_app_terminal:L252')
                try: term["process"].kill()
                except Exception:
                    swallow.note('app_processes.stop_app_terminal:L255')
                    pass
            if term.get("use_pty"):
                try: os.close(term.get("master_fd", -1))
                except Exception:
                    swallow.note('app_processes.stop_app_terminal:L260')
                    pass
            del _sb._app_terminals[app_id]
            return {"ok": True}
    return {"error": "Not running"}

def stop_app_server(app_id):
    """Stop a local server for an app."""
    global _app_servers
    
    with _sb._app_server_lock:
        if app_id in _sb._app_servers:
            try:
                _sb._app_servers[app_id]["process"].terminate()
                _sb._app_servers[app_id]["process"].wait(timeout=5)
            except Exception:
                swallow.note('app_processes.stop_app_server:L276')
                _sb._app_servers[app_id]["process"].kill()
            del _sb._app_servers[app_id]
            _sb.logger.info(f"App server stopped for {app_id}")
            return {"ok": True}
    
    return {"error": "Сервер не запущен"}

def get_app_server_status(app_id):
    """Get server status for an app."""
    with _sb._app_server_lock:
        if app_id in _sb._app_servers:
            server = _sb._app_servers[app_id]
            return {
                "running": True,
                "port": server["port"],
                "url": f"http://localhost:{server['port']}",
                "idle_seconds": int(time.time() - server["last_activity"])
            }
    return {"running": False}

def get_app_terminal_output(app_id):
    """Get terminal output for an app."""
    global _app_terminals
    with _sb._app_terminal_lock:
        if app_id not in _sb._app_terminals:
            return {"output": "", "running": False}
        term = _sb._app_terminals[app_id]
        output_buffer = term["output_buffer"]
        running = term.get("running", True)
        full_output = "".join(output_buffer)
        return {"output": full_output, "running": running}

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
            swallow.note('app_processes.reader:L341')
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    _sb._app_terminals[app_id] = {
        "process": proc,
        "master_fd": None,
        "output_buffer": output_buffer,
        "reader_thread": t,
        "running": True,
        "use_pty": False,
    }
    return {"ok": True}
