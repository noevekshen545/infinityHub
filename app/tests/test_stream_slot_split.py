"""Признак долгого ответа: SSE и chunked не должны занимать общий слот.

main.py нельзя импортировать (он поднимает приложение), поэтому функция
извлекается из исходника через ast — тем же приёмом, что в test_lan_guard.
"""

import ast
import os

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_SRC = os.path.join(APP, "main.py")


def _load_helper():
    with open(MAIN_SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=MAIN_SRC)
    node = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "_stream_response_start"),
        None,
    )
    assert node is not None, "в main.py пропала _stream_response_start"
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, MAIN_SRC, "exec"), namespace)
    return namespace["_stream_response_start"]


def _start(ctype=None, length=None, status=200):
    headers = []
    if ctype:
        headers.append((b"content-type", ctype.encode("latin-1")))
    if length is not None:
        headers.append((b"content-length", str(length).encode("latin-1")))
    return {"type": "http.response.start", "status": status, "headers": headers}


def test_sse_is_stream():
    fn = _load_helper()
    assert fn(_start("text/event-stream")) is True


def test_json_with_length_is_not_stream():
    fn = _load_helper()
    assert fn(_start("application/json", length=42)) is False


def test_html_without_length_is_not_stream():
    fn = _load_helper()
    assert fn(_start("text/html; charset=utf-8")) is False


def test_chunked_binary_is_stream():
    fn = _load_helper()
    assert fn(_start("application/octet-stream")) is True


def test_empty_responses_are_not_stream():
    fn = _load_helper()
    assert fn(_start(status=204)) is False
    assert fn(_start(status=304)) is False
