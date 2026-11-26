"""Клиент LLM на поддельном локальном сервере (без модели)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from filldocx.llm import LLMError, LocalLLM, parse_extraction

ANSWER = {"findings": [{"organ": "liver", "status": "norm", "text": "", "slots": [], "quote": "печень норма"}],
          "conclusion": None, "offtopic": [], "unclear": []}


class _Handler(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.received = body
        content = "<think>рассуждения</think>" + json.dumps(ANSWER, ensure_ascii=False)
        data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_request_uses_json_schema_with_template_enums(server, obp):
    ext, raw = LocalLLM(server).extract(obp, "печень норма", None, contrast=False)
    assert ext.findings[0].organ == "liver"
    fmt = _Handler.received["response_format"]
    assert fmt["type"] == "json_schema"
    organ_enum = fmt["json_schema"]["schema"]["properties"]["findings"]["items"]["properties"]["organ"]["enum"]
    assert organ_enum == obp.organ_ids()
    assert _Handler.received["temperature"] == 0.0
    # органы «только с контрастом» не попадают в промпт исследования без контраста
    assert "После внутривенного контрастного усиления" not in _Handler.received["messages"][1]["content"]


def test_remote_server_refused():
    with pytest.raises(LLMError):
        LocalLLM("https://api.example.com/v1")


def test_parse_strips_reasoning_and_code_fences():
    raw = "<think>x</think>\n```json\n" + json.dumps(ANSWER) + "\n```"
    assert parse_extraction(raw).findings[0].quote == "печень норма"
