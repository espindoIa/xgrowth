"""Aprovação pelo celular via Telegram (botões ✅/❌) e envio do relatório diário.

Funciona com execuções agendadas (sem servidor): cada ciclo lê as atualizações pendentes com
getUpdates. Só aceita cliques vindos do chat configurado em TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import requests

from .db import DB


class NullNotifier:
    enabled = False

    def send(self, text: str, buttons=None):
        return None

    def ask_post(self, post_id: int, tweets: list[str], meta: str):
        return None

    def ask_reply(self, mention_id: str, author: str, mention: str, reply: str, context: str | None):
        return None

    def poll(self) -> list[tuple[str, str, str]]:
        return []


class TelegramNotifier:
    enabled = True

    def __init__(self, token: str, chat_id: str, db: DB, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.db = db
        self.s = session or requests.Session()

    def _call(self, method: str, **params) -> dict:
        r = self.s.post(f"{self.base}/{method}", json=params, timeout=20)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def send(self, text: str, buttons: list[list[dict]] | None = None):
        params = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", **params)

    def ask_post(self, post_id: int, tweets: list[str], meta: str):
        body = "\n\n".join(f"[{i}/{len(tweets)}] {t}" if len(tweets) > 1 else t for i, t in enumerate(tweets, 1))
        text = f"📝 Post #{post_id} para aprovar\n{meta}\n\n{body}"
        return self.send(text, [[
            {"text": "✅ Publicar", "callback_data": f"p:{post_id}:ok"},
            {"text": "❌ Descartar", "callback_data": f"p:{post_id}:no"},
        ]])

    def ask_reply(self, mention_id: str, author: str, mention: str, reply: str, context: str | None):
        ctx = f"Seu post: {context[:200]}\n\n" if context else ""
        text = f"💬 Resposta para aprovar\n{ctx}@{author}: {mention}\n\n➡️ {reply}"
        return self.send(text, [[
            {"text": "✅ Responder", "callback_data": f"r:{mention_id}:ok"},
            {"text": "❌ Ignorar", "callback_data": f"r:{mention_id}:no"},
        ]])

    def _command(self, msg: dict) -> tuple[str, str, str] | None:
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return None  # só o dono do bot pode alimentar o robô
        text = (msg.get("text") or "").strip()
        head, _, rest = text.partition(" ")
        head = head.split("@", 1)[0].lower()  # "/fato@meubot" -> "/fato"
        rest = rest.strip()
        if head == "/fato" and rest:
            return ("f", rest[:300], "add")
        if head == "/fatos":
            return ("f", "", "list")
        if head == "/esquecer":
            return ("f", "", "clear")
        return None

    def poll(self) -> list[tuple[str, str, str]]:
        """Retorna [(tipo, referência, ação)] do chat autorizado:
        cliques 'p'/'r' + 'ok'/'no', e comandos de texto 'f' (/fato, /fatos, /esquecer) + 'add'/'list'/'clear'."""
        offset = self.db.get("tg_offset")
        params = {"timeout": 0, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            params["offset"] = offset
        updates = self._call("getUpdates", **params)
        decisions = []
        for u in updates:
            offset = u["update_id"] + 1
            if u.get("message"):
                cmd = self._command(u["message"])
                if cmd:
                    decisions.append(cmd)
                continue
            cq = u.get("callback_query")
            if not cq:
                continue
            msg = cq.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id"))
            if chat != self.chat_id:
                continue  # ignora cliques de qualquer outro chat
            parts = (cq.get("data") or "").split(":")
            if len(parts) != 3 or parts[0] not in ("p", "r") or parts[2] not in ("ok", "no"):
                continue
            decisions.append((parts[0], parts[1], parts[2]))
            try:
                self._call("answerCallbackQuery", callback_query_id=cq["id"],
                           text="Aprovado" if parts[2] == "ok" else "Descartado")
                label = "✅ aprovado" if parts[2] == "ok" else "❌ descartado"
                self._call("editMessageText", chat_id=self.chat_id, message_id=msg.get("message_id"),
                           text=f"{msg.get('text', '')}\n\n— {label}")
            except Exception:
                pass
        if offset is not None:
            self.db.set("tg_offset", offset)
        return decisions
