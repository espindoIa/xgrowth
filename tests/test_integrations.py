"""Testa os clientes reais (X e Gemini) contra respostas HTTP simuladas — valida o formato
das requisições e a leitura das respostas sem gastar créditos."""
import json
from datetime import datetime, timezone

import pytest

import httpx

from xgrowth.writer import GeminiWriter, PostBrief, ReplyBrief, parse_json_reply
from xgrowth.x_client import XClient, XError


class Resp:
    def __init__(self, status, data, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def x():
    return XClient("k", "s", "t", "ts")


def test_x_me_and_create_post(x):
    calls = []

    def fake(method, url, timeout=None, **kw):
        calls.append((method, url, kw))
        if url.endswith("/users/me"):
            return Resp(200, {"data": {"id": "1", "username": "perfil", "public_metrics": {"followers_count": 12}}})
        return Resp(201, {"data": {"id": "555", "text": kw["json"]["text"]}})

    x.s.request = fake
    assert x.me() == {"id": "1", "username": "perfil", "followers": 12}
    assert x.create_post("oi", reply_to="99", exclude_reply_user_ids=["7"]) == "555"
    method, url, kw = calls[-1]
    assert method == "POST" and url == "https://api.x.com/2/tweets"
    assert kw["json"] == {"text": "oi", "reply": {"in_reply_to_tweet_id": "99", "exclude_reply_user_ids": ["7"]}}


def test_x_own_posts_parses_metrics_and_falls_back_to_public(x):
    seen = []

    def fake(method, url, timeout=None, params=None, **kw):
        seen.append(params["tweet.fields"])
        if "non_public_metrics" in params["tweet.fields"]:
            return Resp(403, {"title": "Forbidden"})
        return Resp(200, {"data": [{"id": "10", "public_metrics": {
            "impression_count": 900, "like_count": 30, "reply_count": 4, "retweet_count": 2,
            "quote_count": 1, "bookmark_count": 5}}]})

    x.s.request = fake
    out = x.own_posts("1", datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert out[0]["impressions"] == 900 and out[0]["likes"] == 30 and out[0]["bookmarks"] == 5
    assert out[0]["profile_clicks"] == 0
    assert x.public_only is True and len(seen) == 2


def test_x_mentions_parse(x):
    def fake(method, url, timeout=None, params=None, **kw):
        assert params["since_id"] == "100"
        return Resp(200, {
            "data": [{"id": "101", "author_id": "42", "text": "@perfil oi", "created_at": "2026-10-11T10:00:00.000Z",
                      "referenced_tweets": [{"type": "replied_to", "id": "50"}],
                      "entities": {"mentions": [{"username": "perfil", "id": "1"}, {"username": "outro", "id": "77"}]}}],
            "includes": {"users": [{"id": "42", "username": "ana"}]},
        })

    x.s.request = fake
    m = x.mentions("1", since_id="100")[0]
    assert m["username"] == "ana" and m["in_reply_to"] == "50" and m["mentioned_ids"] == ["1", "77"]


def test_x_errors_are_explained(x):
    x.s.request = lambda *a, **k: Resp(402, {"title": "Payment Required"})
    with pytest.raises(XError, match="créditos"):
        x.me()
    x.s.request = lambda *a, **k: Resp(429, {}, {"x-rate-limit-reset": "0"})
    with pytest.raises(XError) as e:
        x.me()
    assert e.value.status == 429


def _gemini_with(handler, cfg):
    """GeminiWriter com o SDK oficial de verdade, mas falando com um servidor HTTP simulado."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key="k-test", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler))))
    return GeminiWriter(cfg, "k-test", client=client)


def _gemini_msg(data, finish="STOP"):
    text = data if isinstance(data, str) else json.dumps(data)
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}]}


def test_gemini_writer_post_request_and_parse(cfg):
    sent = {}

    def handler(request: httpx.Request):
        sent["url"] = str(request.url)
        sent["key"] = request.headers.get("x-goog-api-key")
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=_gemini_msg({"tema": "fps", "posts": [" Dica 1 ", ""]}))

    w = _gemini_with(handler, cfg)
    d = w.write_post(PostBrief("dicas_fps", "lista", "direto", "numero"))
    assert d.tweets == ["Dica 1"] and d.tema == "fps"
    assert sent["url"].endswith(f"/models/{cfg.modelo}:generateContent")
    assert sent["key"] == "k-test"
    gen = sent["body"]["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseJsonSchema"]["required"] == ["tema", "posts"]
    assert gen["responseJsonSchema"]["properties"]["posts"]["maxItems"] == cfg.max_tweets_thread
    assert "thinking_level" in json.dumps(gen["thinkingConfig"]).lower()
    assert cfg.nicho in sent["body"]["systemInstruction"]["parts"][0]["text"]


def test_gemini_writer_reply_and_ping(cfg):
    def handler(request):
        body = json.loads(request.content)
        if "responder" in json.dumps(body["generationConfig"]["responseJsonSchema"]):
            return httpx.Response(200, json=_gemini_msg({"responder": True, "texto": "Valeu!", "motivo": "ok"}))
        return httpx.Response(200, json=_gemini_msg({"ok": True}))

    w = _gemini_with(handler, cfg)
    d = w.write_reply(ReplyBrief("ana", "boa dica", "post original"))
    assert d.responder and d.texto == "Valeu!"
    assert w.ping() == "ok"


def test_gemini_empty_answer_explains_why(cfg):
    def handler(request):
        return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})

    with pytest.raises(RuntimeError, match="não retornou texto"):
        _gemini_with(handler, cfg).write_post(PostBrief("a", "b", "c", "d"))


def test_gemini_http_error_is_raised(cfg):
    def handler(request):
        return httpx.Response(429, json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    from google.genai import errors
    with pytest.raises(errors.APIError):
        _gemini_with(handler, cfg).write_post(PostBrief("a", "b", "c", "d"))


def test_parse_json_reply_tolerates_markdown_fence():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_reply("[1, 2]")
