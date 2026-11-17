"""
Управление модулем Hermes Agent из командной строки.

    ~/infinity-venv/bin/python3 hermes_module/agent_ctl.py start|stop|restart|status|logs

Обычно модуль поднимает сам main.py (config.json → hermes.autostart);
нужен для ручной диагностики и проверки без всей системы.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_module.integration import launch  # noqa: E402


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        res = launch.ensure()
        print(res)
        return 0 if res.get("ok") else 1
    if cmd == "stop":
        print(launch.stop())
    elif cmd == "restart":
        print(launch.ensure())
    elif cmd == "logs":
        for f in (launch.config.GATEWAY_LOG, launch.config.FRONTEND_LOG):
            print(f"\n===== {f} =====")
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    print("".join(fh.readlines()[-40:]))
            except Exception as e:
                print("(нет лога)", e)
    else:
        for k, v in launch.status().items():
            print(f"{k:20}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
