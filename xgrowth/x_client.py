"""Cliente mínimo da API v2 do X (OAuth 1.0a, contexto de usuário) + versão de simulação (dry-run).

Endpoints usados e custo pay-per-use (out/2026, docs.x.com/x-api/getting-started/pricing):
  POST /2/tweets                   criar post / resposta        US$ 0,015 por requisição (com URL: US$ 0,20)
  GET  /2/users/me                 seguidores                   US$ 0,010 por usuário lido
  GET  /2/users/:id/tweets         seus posts + métricas        US$ 0,001 por post ("owned read")
  GET  /2/users/:id/mentions       menções e respostas a você   US$ 0,001 por post ("owned read")
Leituras do mesmo recurso no mesmo dia UTC são cobradas uma vez só (deduplicação).
"""
from __future__ import annotations

import itertools
import time
from datetime import datetime, timezone

import requests

API = "https://api.x.com/2"


class XError(Exception):
    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


class XClient:
    def __init__(self, api_key: str, api_secret: str, access_token: str, access_secret: str, timeout: float = 30):
        from requests_oauthlib import OAuth1Session

        self.s = OAuth1Session(api_key, client_secret=api_secret,
                               resource_owner_key=access_token, resource_owner_secret=access_secret)
        self.timeout = timeout
        self.public_only = False

    def _req(self, method: str, path: str, **kw) -> dict:
        url = f"{API}{path}"
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                if attempt == 2:
                    raise XError(f"falha de rede: {e}") from e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                reset = r.headers.get("x-rate-limit-reset")
                wait = max(float(reset) - time.time(), 0) if reset else None
                raise XError("limite de requisições atingido (429)", 429, wait)
            if r.status_code >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text}
            if r.status_code >= 400:
                detail = data.get("detail") or data.get("title") or data.get("errors") or data
                hint = ""
                if r.status_code == 402:
                    hint = " — créditos da API esgotados: recarregue em console.x.com"
                elif r.status_code == 403:
                    hint = " — verifique se o app tem permissão Read+Write e se os tokens foram gerados depois disso"
                raise XError(f"X API {r.status_code}: {detail}{hint}", r.status_code)
            return data
        raise XError("falha após 3 tentativas")  # pragma: no cover

    # ---- leitura ------------------------------------------------------------
    def me(self) -> dict:
        d = self._req("GET", "/users/me", params={"user.fields": "public_metrics,created_at"})["data"]
        return {"id": d["id"], "username": d["username"],
                "followers": d.get("public_metrics", {}).get("followers_count", 0)}

    def own_posts(self, user_id: str, since: datetime) -> list[dict]:
        """Posts próprios (sem respostas/reposts) desde `since`, com métricas públicas e privadas."""
        params = {
            "max_results": 100,
            "exclude": "replies,retweets",
            "start_time": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tweet.fields": "created_at,public_metrics" + ("" if self.public_only else ",non_public_metrics"),
        }
        try:
            data = self._req("GET", f"/users/{user_id}/tweets", params=params)
        except XError as e:
            if self.public_only or e.status not in (400, 403):
                raise
            # sem acesso às métricas privadas (cliques no perfil): segue só com as públicas
            self.public_only = True
            params["tweet.fields"] = "created_at,public_metrics"
            data = self._req("GET", f"/users/{user_id}/tweets", params=params)
        out = []
        for t in data.get("data", []) or []:
            pm = t.get("public_metrics", {}) or {}
            npm = t.get("non_public_metrics", {}) or {}
            out.append({
                "id": t["id"],
                "impressions": pm.get("impression_count", npm.get("impression_count", 0)),
                "likes": pm.get("like_count", 0),
                "replies": pm.get("reply_count", 0),
                "reposts": pm.get("retweet_count", 0),
                "quotes": pm.get("quote_count", 0),
                "bookmarks": pm.get("bookmark_count", 0),
                "profile_clicks": npm.get("user_profile_clicks", 0),
                "url_clicks": npm.get("url_link_clicks", 0),
            })
        return out

    def mentions(self, user_id: str, since_id: str | None = None, max_results: int = 50) -> list[dict]:
        params = {
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": "author_id,created_at,conversation_id,referenced_tweets,entities",
            "expansions": "author_id",
            "user.fields": "username",
        }
        if since_id:
            params["since_id"] = since_id
        data = self._req("GET", f"/users/{user_id}/mentions", params=params)
        users = {u["id"]: u["username"] for u in (data.get("includes", {}) or {}).get("users", [])}
        out = []
        for t in data.get("data", []) or []:
            reply_to = next((r["id"] for r in t.get("referenced_tweets", []) or [] if r.get("type") == "replied_to"), None)
            mentioned = [m.get("id") for m in (t.get("entities", {}) or {}).get("mentions", []) or [] if m.get("id")]
            out.append({
                "id": t["id"], "author_id": t.get("author_id"), "username": users.get(t.get("author_id"), ""),
                "text": t.get("text", ""), "created_at": t.get("created_at"),
                "in_reply_to": reply_to, "mentioned_ids": mentioned,
            })
        return out

    # ---- escrita ------------------------------------------------------------
    def create_post(self, text: str, reply_to: str | None = None, exclude_reply_user_ids: list[str] | None = None) -> str:
        body: dict = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
            if exclude_reply_user_ids:
                body["reply"]["exclude_reply_user_ids"] = exclude_reply_user_ids
        return self._req("POST", "/tweets", json=body)["data"]["id"]


class DryRunXClient:
    """Não toca no X: registra o que seria publicado. Útil para testar geração, agenda e aprovações."""

    def __init__(self, username: str = "perfil_teste", followers: int = 0, clock=None, verbose: bool = True):
        self._ids = itertools.count(1_000_000)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.verbose = verbose
        self.username = username
        self.followers = followers
        self.published: list[dict] = []
        self.fake_metrics: dict[str, dict] = {}
        self.fake_mentions: list[dict] = []

    def me(self) -> dict:
        return {"id": "999", "username": self.username, "followers": self.followers}

    def own_posts(self, user_id: str, since: datetime) -> list[dict]:
        out = []
        for p in self.published:
            if p["reply_to"] is None and p["at"] >= since and p["id"] in self.fake_metrics:
                out.append({"id": p["id"], **self.fake_metrics[p["id"]]})
        return out

    def mentions(self, user_id: str, since_id: str | None = None, max_results: int = 50) -> list[dict]:
        ms = [m for m in self.fake_mentions if since_id is None or int(m["id"]) > int(since_id)]
        return sorted(ms, key=lambda m: int(m["id"]), reverse=True)[:max_results]

    def create_post(self, text: str, reply_to: str | None = None, exclude_reply_user_ids: list[str] | None = None) -> str:
        tid = f"{next(self._ids)}"
        self.published.append({"id": tid, "text": text, "reply_to": reply_to,
                               "exclude": exclude_reply_user_ids or [], "at": self.clock()})
        if self.verbose:
            tag = f"resposta a {reply_to}" if reply_to else "post"
            print(f"  [dry-run] {tag} {tid}: {text}", flush=True)
        return tid
