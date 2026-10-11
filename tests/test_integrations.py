"""Testa os clientes reais (X e Claude) contra respostas HTTP simuladas — valida o formato
das requisições e a leitura das respostas sem gastar créditos."""
import json
from datetime import datetime, timezone

import pytest

try:  # versões recentes do SDK da Anthropic usam o pacote httpx2
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from xgrowth.writer import ClaudeWriter, PostBrief, ReplyBrief
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


def _claude_with(handler, cfg):
    w = ClaudeWriter(cfg, "sk-test")
    import anthropic
    w.client = anthropic.Anthropic(api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return w


def _tool_msg(name, data):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "tool_use", "id": "tu_1", "name": name, "input": data}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def test_claude_writer_post_request_and_parse(cfg):
    sent = {}

    def handler(request: httpx.Request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=_tool_msg("entregar_post", {"tema": "fps", "posts": [" Dica 1 ", ""]}))

    w = _claude_with(handler, cfg)
    d = w.write_post(PostBrief("dicas_fps", "lista", "direto", "numero"))
    assert d.tweets == ["Dica 1"] and d.tema == "fps"
    assert sent["model"] == cfg.modelo
    assert sent["tool_choice"] == {"type": "tool", "name": "entregar_post"}
    assert sent["tools"][0]["name"] == "entregar_post"
    assert cfg.nicho in sent["system"]


def test_claude_writer_reply(cfg):
    def handler(request):
        return httpx.Response(200, json=_tool_msg("decidir_resposta", {"responder": True, "texto": "Valeu!", "motivo": "ok"}))

    d = _claude_with(handler, cfg).write_reply(ReplyBrief("ana", "boa dica", "post original"))
    assert d.responder and d.texto == "Valeu!"
