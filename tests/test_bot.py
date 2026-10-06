import json
import os
import sys
import types

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("LOG_PUBLIC_URL", "https://example.com/run.jsonl")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot  # noqa: E402


def test_extract_json_plain():
    assert bot.extract_json('{"answer": 1}') == {"answer": 1}


def test_extract_json_code_fence_and_chatter():
    assert bot.extract_json('```json\n{"answer": {"x": 2}}\n```') == {"answer": {"x": 2}}
    assert bot.extract_json('Sure! Here: {"answer": 3} hope it helps') == {"answer": 3}


def test_extract_json_rejects_garbage():
    assert bot.extract_json("no json here") is None
    assert bot.extract_json("[1, 2, 3]") is None
    assert bot.extract_json("") is None


def test_normalize_forces_log_url_and_drops_extras():
    out = bot.normalize_reply({"answer": 5, "log_url": "evil", "extra": 1})
    assert out == {"answer": 5, "log_url": bot.LOG_PUBLIC_URL}
    assert bot.normalize_reply({"nope": 1}) is None


def test_ssrf_blocks_private_hosts(tmp_path):
    for url in ["http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest", "ftp://example.com/x", "file:///etc/passwd"]:
        assert bot.tool_fetch_url(url, str(tmp_path)).startswith("ERROR")


def test_run_python_computes(tmp_path):
    (tmp_path / "d.csv").write_text("a,b\n1,2\n3,4\n")
    out = bot.tool_run_python("import pandas as pd\nprint(pd.read_csv('d.csv')['b'].sum())", str(tmp_path))
    assert out.strip() == "6"


def test_run_python_blocks_dangerous_code(tmp_path):
    for code in ["import os\nprint(os.environ)", "import subprocess", "import socket", "open('/proc/self/environ')"]:
        assert bot.tool_run_python(code, str(tmp_path)).startswith("ERROR")


def test_run_python_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "PY_TIMEOUT", 2)
    assert "timed out" in bot.tool_run_python("while True: pass", str(tmp_path))


def _fake_response(content=None, tool_calls=None):
    msg = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_agent_uses_tool_then_answers(monkeypatch):
    tc = types.SimpleNamespace(
        id="1", function=types.SimpleNamespace(name="run_python", arguments=json.dumps({"code": "print(21*2)"}))
    )
    responses = iter([_fake_response(tool_calls=[tc]), _fake_response(content='```json\n{"answer": 42, "log_url": "x"}\n```')])
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: next(responses))
    reply, steps, _ = bot.run_agent("what is 21*2? reply with json")
    assert reply == {"answer": 42, "log_url": bot.LOG_PUBLIC_URL}
    assert steps[0]["tool"] == "run_python" and steps[0]["result"].strip() == "42"


def test_agent_survives_model_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(bot, "call_model", boom)
    reply, _, _ = bot.run_agent("hi")
    assert reply["answer"]["error"] == "model_call_failed"
    assert reply["log_url"] == bot.LOG_PUBLIC_URL


def test_agent_repairs_bad_output(monkeypatch):
    responses = iter([
        _fake_response(content="the answer is 7"),     # first reply: not JSON
        _fake_response(content="still not json"),      # forced final: not JSON
        _fake_response(content='{"answer": 7}'),       # repair succeeds
    ])
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: next(responses))
    reply, _, _ = bot.run_agent("q")
    assert reply == {"answer": 7, "log_url": bot.LOG_PUBLIC_URL}


def test_rate_limit_falls_back_to_secondary_model(monkeypatch):
    from openai import RateLimitError
    import httpx

    calls = []

    def fake_create(model, kwargs):
        calls.append(model)
        if model == bot.GROQ_MODEL:
            req = httpx.Request("POST", "https://x")
            raise RateLimitError("limit", response=httpx.Response(429, request=req), body=None)
        return _fake_response(content='{"answer": 1}')

    monkeypatch.setattr(bot, "client", object())
    monkeypatch.setattr(bot, "_create", fake_create)
    monkeypatch.setattr(bot, "_primary_blocked_until", 0.0)
    bot.call_model([{"role": "user", "content": "hi"}])
    assert calls == [bot.GROQ_MODEL, bot.GROQ_FALLBACK_MODEL]
    bot.call_model([{"role": "user", "content": "hi"}])  # primary is now in cooldown
    assert calls[-1] == bot.GROQ_FALLBACK_MODEL and calls.count(bot.GROQ_MODEL) == 1


def test_retired_model_404_falls_back(monkeypatch):
    from openai import NotFoundError
    import httpx

    calls = []

    def fake_create(model, kwargs):
        calls.append(model)
        if model == bot.GROQ_MODEL:
            req = httpx.Request("POST", "https://x")
            raise NotFoundError("model_not_found", response=httpx.Response(404, request=req), body=None)
        return _fake_response(content='{"answer": 1}')

    monkeypatch.setattr(bot, "client", object())
    monkeypatch.setattr(bot, "_create", fake_create)
    monkeypatch.setattr(bot, "_primary_blocked_until", 0.0)
    bot.call_model([{"role": "user", "content": "hi"}])
    assert calls == [bot.GROQ_MODEL, bot.GROQ_FALLBACK_MODEL]
    assert bot._primary_blocked_until > 0
