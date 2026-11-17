"""Каналы команд: новый WebSocket и старый путь как фолбэк.

Проверяется главное обещание правки: WS отдаёт те же команды, что и раньше
отдавали SSE/long-poll, виден в телеметрии, а выключателем
INFINITY_WS_CHANNELS=0 закрывается, оставляя клиентам старый транспорт.
"""
import os
import sys
import time
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


class WsChannels(unittest.TestCase):
    def setUp(self):
        os.environ.pop("INFINITY_WS_CHANNELS", None)
        import player_api
        import desktop_bridge
        import routes.hotkeys as hotkeys
        self.player = player_api
        self.bridge = desktop_bridge
        self.hotkeys = hotkeys
        self.player._subscribers.clear()
        self.player._commands.clear()
        self.player._acks.clear()
        with self.bridge._lock:
            self.bridge._pending.clear()
        app = FastAPI()
        app.include_router(player_api.router)
        app.include_router(hotkeys.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.player._subscribers.clear()
        self.player._commands.clear()
        self.player._acks.clear()
        with self.bridge._lock:
            self.bridge._pending.clear()
        os.environ.pop("INFINITY_WS_CHANNELS", None)

    def test_player_ws_gets_pending_command(self):
        self.player._remember({"id": "c1", "action": "pause", "ts": time.time(),
                               "profile": ""})
        with self.client.websocket_connect("/api/player/mini/ws") as ws:
            hello = ws.receive_json()
            self.assertEqual(hello["event"], "hello")
            self.assertEqual(hello["transport"], "ws")
            self.assertGreaterEqual(hello["pending"], 1)
            message = ws.receive_json()
            self.assertEqual(message["event"], "mini")
            self.assertEqual(message["data"]["id"], "c1")
            self.assertEqual(self.player._subscriber_count("ws"), 1)
            self.assertEqual(self.player._subscriber_count("sse"), 0)
        self.assertEqual(self.player._subscriber_count("ws"), 0)

    def test_player_status_reports_transport(self):
        with self.client.websocket_connect("/api/player/mini/ws"):
            status = self.client.get("/api/player/status").json()
            self.assertEqual(status["transport"]["ws"], 1)
            self.assertTrue(status["transport"]["ws_enabled"])

    def test_desktop_ws_delivers_and_acks(self):
        with self.client.websocket_connect("/api/desktop/ws") as ws:
            hello = ws.receive_json()
            self.assertEqual(hello["event"], "hello")
            self.assertEqual(hello["transport"], "ws")
            self.assertEqual(self.bridge.status()["ws_active"], 1)
            command = self.bridge.push("open-window", {"url": "/desktop"})
            message = ws.receive_json()
            self.assertEqual(message["commands"][0]["id"], command["id"])
            ws.send_json({"ack": [command["id"]]})
            for _ in range(20):
                if self.bridge.status()["pending"] == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(self.bridge.status()["pending"], 0)
        for _ in range(20):
            if self.bridge.status()["ws_active"] == 0:
                break
            time.sleep(0.05)
        self.assertEqual(self.bridge.status()["ws_active"], 0)

    def test_kill_switch_closes_socket(self):
        os.environ["INFINITY_WS_CHANNELS"] = "0"
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/api/player/mini/ws") as ws:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, 4404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
