"""Флаги секции stats из Настроек должны что-то менять.

До этой правки два переключателя были декоративными: stats.enabled
сохранялся, но track_action писал историю всегда, а stats.ai_recommendations
никак не влиял на /api/stats/recommendations/ai — запрос уходил в LLM при
любом положении тумблера.

Импортировать routes/stats.py нельзя: он тянет server и поднимает всё
приложение, поэтому берём с диска исходник тестируемых функций через ast —
тот же приём, что в test_config_save_rules.py.
"""
import ast
import os
import sys
import types
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import swallow

_SRC = os.path.join(APP, "modules", "stats_recommend.py")


def _load_stats_enabled(fake_cfg):
    """Собрать _stats_enabled из исходника с подменённым config_module."""
    with open(_SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_stats_enabled"]
    ns = {"swallow": swallow}
    exec(compile(ast.Module(body=body, type_ignores=[]), _SRC, "exec"), ns)
    fake = types.ModuleType("config_module")
    fake.get_config = lambda: fake_cfg
    real = sys.modules.get("config_module")
    sys.modules["config_module"] = fake
    try:
        return ns["_stats_enabled"]()
    finally:
        if real is not None:
            sys.modules["config_module"] = real
        else:
            sys.modules.pop("config_module", None)


class StatsEnabled(unittest.TestCase):
    def test_default_on_when_key_absent(self):
        """Нет ключа — сбор включён: выключение должно быть осознанным."""
        self.assertTrue(_load_stats_enabled({"stats": {}}))

    def test_off(self):
        self.assertFalse(_load_stats_enabled({"stats": {"enabled": False}}))

    def test_on(self):
        self.assertTrue(_load_stats_enabled({"stats": {"enabled": True}}))

    def test_no_stats_section(self):
        self.assertTrue(_load_stats_enabled({}))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class AiRecommendationsGate(unittest.TestCase):
    """Выключенный флаг обязан отвечать без обращения к LLM."""

    def _call(self, stats_cfg, llm_calls):
        import routes.stats as S
        orig_cfg = S.server.CFG
        orig_engine = S._engine

        class _Eng:
            def _get_interest_profile(self, **_kw):
                return {}, {}

            def get_ai_recommendations(self, *a, **kw):
                llm_calls.append(1)
                return [{"title": "T"}]

        S.server.CFG = {"stats": stats_cfg}
        S._engine = lambda: _Eng()
        try:
            return S.stats_recommend_ai(module="books", count=3, days=30)
        finally:
            S.server.CFG = orig_cfg
            S._engine = orig_engine

    def test_disabled_makes_no_llm_call(self):
        calls = []
        out = self._call({"ai_recommendations": False}, calls)
        self.assertEqual(calls, [])
        self.assertTrue(out.get("disabled"))
        self.assertEqual(out["items"], [])
        self.assertFalse(out["ai_generated"])

    def test_absent_defaults_off(self):
        """Дефолт config_module — False, значит и поведение по умолчанию False."""
        calls = []
        out = self._call({}, calls)
        self.assertEqual(calls, [])
        self.assertTrue(out.get("disabled"))

    def test_enabled_calls_engine(self):
        calls = []
        out = self._call({"ai_recommendations": True}, calls)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("disabled", out)
        self.assertTrue(out["ai_generated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
