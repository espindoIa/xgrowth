"""O ciclo do robô. Cada `tick()` é idempotente: pode rodar a cada 5–30 minutos (cron, GitHub
Actions, Agendador de Tarefas do Windows) e só faz o que estiver pendente:

  1. atualiza seguidores            5. publica o que chegou na hora
  2. processa aprovações            6. coleta métricas dos posts recentes
  3. planeja os horários do dia     7. responde quem interagiu (com regras)
  4. escreve os rascunhos           8. relatório diário
"""
from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .bandit import choose_content, choose_hours
from .config import Config
from .db import DB, iso, parse_iso
from .learning import build_model, compute_reward, mature_metrics
from .notifier import NullNotifier
from .textutil import matches_opt_out, max_similarity, problems_for_post, problems_for_reply
from .writer import PostBrief, ReplyBrief
from .x_client import XError

PENDING_POST = ("planned", "awaiting_approval", "approved", "drafted")


class Engine:
    def __init__(self, cfg: Config, db: DB, x, writer, notifier=None, clock=None, seed: int | None = None,
                 report_path: str | None = "data/relatorio.md"):
        self.cfg = cfg
        self.db = db
        self.x = x
        self.writer = writer
        self.notifier = notifier or NullNotifier()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tz = ZoneInfo(cfg.fuso)
        self.rng = random.Random(seed)
        self.seed = seed
        self.report_path = report_path
        self._model = None

    # ------------------------------------------------------------------ util
    def now(self) -> datetime:
        return self.clock()

    def local(self, dt: datetime | None = None) -> datetime:
        return (dt or self.now()).astimezone(self.tz)

    def log(self, level: str, msg: str) -> None:
        self.db.log(level, msg, self.now())

    def model(self):
        if self._model is None:
            seed = None if self.seed is None else self.rng.randrange(2**31)
            self._model = build_model(self.db, self.cfg, self.now(), seed=seed)
        return self._model

    def me(self) -> dict:
        me = self.db.get("me")
        if not me:
            raise RuntimeError("perfil ainda não sincronizado")
        return me

    # ------------------------------------------------------------------ ciclo
    def tick(self) -> dict:
        self._model = None
        if self.cfg.pausado:
            self.log("info", "robô pausado (seguranca.pausado = true)")
            return {"pausado": True}
        stats: dict = {}
        steps = [
            ("perfil", self._sync_profile),
            ("aprovacoes", self._process_approvals),
            ("plano", self._plan_today),
            ("rascunhos", self._draft_upcoming),
            ("publicacao", self._publish_due),
            ("metricas", self._collect_metrics),
            ("respostas", self._handle_mentions),
            ("relatorio", self._daily_report),
        ]
        for name, fn in steps:
            try:
                stats[name] = fn()
            except XError as e:
                self.log("erro", f"{name}: {e}")
                stats[name] = f"erro: {e}"
                if e.status in (401, 402):
                    break  # credenciais ou créditos: não adianta continuar
            except Exception as e:  # um passo com problema não derruba os outros
                self.log("erro", f"{name}: {type(e).__name__}: {e}")
                stats[name] = f"erro: {e}"
        return stats

    # ------------------------------------------------------------------ 1. perfil
    def _sync_profile(self):
        now = self.now()
        last = self.db.get("followers_checked_at")
        if self.db.get("me") and last and now - parse_iso(last) < timedelta(minutes=55):
            return "ok (recente)"
        info = self.x.me()
        self.db.set("me", {"id": info["id"], "username": info["username"]})
        self.db.x("INSERT INTO followers(at, count) VALUES(?, ?)", iso(now), int(info["followers"]))
        self.db.set("followers_checked_at", iso(now))
        return f"@{info['username']}: {info['followers']} seguidores"

    # ------------------------------------------------------------------ 2. aprovações
    # ---- fatos do dia (config + comandos /fato do Telegram) -------------------
    def fatos_vigentes(self) -> list[str]:
        """Fatos fixos do config + os enviados por /fato que ainda não expiraram."""
        limit = self.now() - timedelta(days=self.cfg.validade_fatos_dias)
        extra = [f["texto"] for f in (self.db.get("fatos_extra") or []) if parse_iso(f["em"]) >= limit]
        return list(self.cfg.fatos) + extra

    def _fact_command(self, text: str, action: str) -> str:
        now = self.now()
        limit = now - timedelta(days=self.cfg.validade_fatos_dias)
        keep = [f for f in (self.db.get("fatos_extra") or []) if parse_iso(f["em"]) >= limit]
        if action == "add":
            keep = (keep + [{"em": iso(now), "texto": text}])[-12:]
            self.db.set("fatos_extra", keep)
            return (f"✅ Fato anotado (vale por {self.cfg.validade_fatos_dias} dias): {text}\n"
                    "Ele entra nos próximos rascunhos. Só afirme o que for verdade: o robô vai tratar como certo.")
        if action == "clear":
            self.db.set("fatos_extra", [])
            return "🧹 Fatos enviados por você foram apagados (os do config.yaml continuam)."
        self.db.set("fatos_extra", keep)
        atuais = self.fatos_vigentes()
        return "Fatos em uso:\n" + ("\n".join(f"- {f}" for f in atuais) if atuais else "(nenhum)")

    def decide(self, kind: str, ref: str, decision: str) -> str:
        if kind == "f":
            return self._fact_command(ref, decision)
        ok = decision == "ok"
        if kind == "p":
            row = self.db.one("SELECT status FROM posts WHERE id=?", int(ref))
            if row and row["status"] == "awaiting_approval":
                self.db.update_post(int(ref), status="approved" if ok else "rejected")
                return f"post #{ref} {'aprovado' if ok else 'descartado'}"
        elif kind == "r":
            row = self.db.one("SELECT status FROM mentions WHERE tweet_id=?", ref)
            if row and row["status"] == "awaiting_approval":
                self.db.update_mention(ref, status="approved" if ok else "rejected")
                return f"resposta a {ref} {'aprovada' if ok else 'descartada'}"
        return f"{kind}:{ref} ignorado (já decidido ou inexistente)"

    def _process_approvals(self):
        msgs = []
        for k, r, d in self.notifier.poll():
            msg = self.decide(k, r, d)
            msgs.append(msg)
            if k == "f" and self.notifier.enabled:
                self.notifier.send(msg)
        sent = self._send_approved_replies()
        return {"decisoes": msgs, "respostas_enviadas": sent}

    def _send_approved_replies(self) -> int:
        limit = iso(self.now() - timedelta(hours=36))
        for m in self.db.q("SELECT tweet_id FROM mentions WHERE status='awaiting_approval' AND handled_at<?", limit):
            self.db.update_mention(m["tweet_id"], status="expired", reason="sem aprovação em 36h")
        n = 0
        for m in self.db.q("SELECT * FROM mentions WHERE status='approved' ORDER BY tweet_id"):
            if m["created_at"] and self.now() - parse_iso(m["created_at"]) > timedelta(hours=36):
                self.db.update_mention(m["tweet_id"], status="expired", reason="aprovada tarde demais")
                continue
            self._post_reply(m, m["reply_text"])
            n += 1
        return n

    # ------------------------------------------------------------------ 3. plano do dia
    def _plan_today(self):
        local_now = self.local()
        date = local_now.date().isoformat()
        if self.db.get(f"plan:{date}") is not None:
            return "já planejado"
        # O minuto é sorteado cedo o bastante para o ciclo seguinte ainda cair dentro da mesma hora
        # (senão o post planejado para 16h sairia 17h e o otimizador aprenderia a hora errada).
        last_minute = max(0, 55 - self.cfg.intervalo_ciclo_min)
        earliest = local_now + timedelta(minutes=10)
        allowed, first_minute = [], {}
        for h in self.cfg.horas:
            slot_start = datetime.combine(local_now.date(), time(h, 0), tzinfo=self.tz)
            if slot_start.replace(minute=last_minute) < earliest:
                continue  # esse horário já passou (ou não dá mais tempo hoje)
            allowed.append(h)
            first_minute[h] = 0 if slot_start >= earliest else earliest.minute
        n = min(self.cfg.posts_por_dia, self.cfg.max_posts_dia_absoluto)
        hours = choose_hours(self.model(), allowed, n, self.cfg.espacamento_min_horas, self.cfg.exploracao, self.rng)
        ids = []
        for h, explored in hours:
            minute = self.rng.randint(first_minute[h], max(first_minute[h], last_minute))
            when = datetime.combine(local_now.date(), time(h, minute), tzinfo=self.tz)
            ids.append(self.db.x(
                "INSERT INTO posts(plan_date, scheduled_at, hour, hour_explored, status, created_at) VALUES(?,?,?,?,?,?)",
                date, iso(when), h, int(explored), "planned", iso(self.now()),
            ))
        self.db.set(f"plan:{date}", ids)
        txt = ", ".join(f"{h}h{'*' if e else ''}" for h, e in hours)
        self.log("info", f"plano de {date}: {txt or 'nenhum horário disponível'} (* = horário em teste)")
        return txt

    # ------------------------------------------------------------------ 4. rascunhos
    def _draft_upcoming(self):
        ahead = self.cfg.antecedencia_aprovacao_horas if self.cfg.modo_posts == "aprovar" else self.cfg.antecedencia_rascunho_horas
        limit = iso(self.now() + timedelta(hours=ahead))
        rows = self.db.q("SELECT * FROM posts WHERE status='planned' AND scheduled_at<=? ORDER BY scheduled_at", limit)
        done = []
        for row in rows:
            done.append(self._draft(row))
        return done

    def _recent_texts(self, n: int = 30) -> list[str]:
        rows = self.db.q("SELECT tweets FROM posts WHERE tweets IS NOT NULL AND status IN "
                         "('posted','drafted','approved','awaiting_approval') ORDER BY id DESC LIMIT ?", n)
        return [" / ".join(json.loads(r["tweets"])) for r in rows]

    def _best_texts(self, n: int = 5) -> list[str]:
        now = self.now()
        scored = []
        for p in self.db.q("SELECT * FROM posts WHERE status='posted' AND posted_at IS NOT NULL"):
            age_h = (now - parse_iso(p["posted_at"])).total_seconds() / 3600
            m = mature_metrics(self.db, p["id"], self.cfg.horizonte_horas, age_h)
            if m is not None and p["tweets"]:
                scored.append((compute_reward(dict(m), self.cfg), " / ".join(json.loads(p["tweets"]))))
        scored.sort(reverse=True)
        return [t for _, t in scored[:n]]

    def _draft(self, row) -> str:
        dims = {k: v for k, v in self.cfg.dimensoes().items() if k != "hora"}
        fatos = self.fatos_vigentes()
        if not fatos:  # pilares de crítica/atualidade só entram em campo com fato que os sustente
            dims["pilar"] = [p for p in dims["pilar"] if p not in self.cfg.pilares_requer_fatos]
        today = [{"pilar": r["pillar"], "formato": r["format"]} for r in
                 self.db.q("SELECT pillar, format FROM posts WHERE plan_date=? AND pillar IS NOT NULL", row["plan_date"])]
        choice, explored = choose_content(self.model(), dims, self.cfg.exploracao, self.rng, avoid=today)
        recent = self._recent_texts()
        brief = PostBrief(pilar=choice["pilar"], formato=choice["formato"], tom=choice["tom"],
                          gancho=choice["gancho"], recentes=recent, melhores=self._best_texts(),
                          dia_de_jogo=row["plan_date"] in self.cfg.dias_de_jogo, fatos=fatos)
        max_tweets = self.cfg.max_tweets_thread if choice["formato"] == "thread" else 1
        draft, probs = None, ["não gerado"]
        try:
            for _ in range(3):
                draft = self.writer.write_post(brief)
                probs = problems_for_post(draft.tweets, max_tweets, fatos=fatos)
                if choice["formato"] == "thread" and len(draft.tweets) < 2:
                    probs.append("thread precisa de pelo menos 2 posts")
                sim = max_similarity(" ".join(draft.tweets), recent)
                if sim > 0.5:
                    probs.append(f"parecido demais com um post recente (similaridade {sim:.2f}); mude tema e estrutura")
                if not probs:
                    break
                brief.feedback = probs
        except Exception as e:
            probs = [f"{type(e).__name__}: {e}"]

        attempts = (row["attempts"] or 0) + 1
        if probs:
            status = "failed" if attempts >= 2 else "planned"
            self.db.update_post(row["id"], attempts=attempts, error="; ".join(probs), status=status)
            self.log("aviso", f"post #{row['id']}: rascunho rejeitado ({'; '.join(probs)})")
            return f"#{row['id']} rejeitado"

        status = "awaiting_approval" if self.cfg.modo_posts == "aprovar" else "drafted"
        self.db.update_post(
            row["id"], pillar=choice["pilar"], format=choice["formato"], tone=choice["tom"], hook=choice["gancho"],
            content_explored=int(explored), topic=draft.tema, tweets=draft.tweets, status=status,
            attempts=attempts, error=None,
        )
        if status == "awaiting_approval":
            local = self.local(parse_iso(row["scheduled_at"]))
            meta = (f"{local:%d/%m %H:%M} · {choice['pilar']} · {choice['formato']} · {choice['tom']} · "
                    f"{choice['gancho']}{' · (teste)' if explored else ''}")
            if self.notifier.enabled:
                self.notifier.ask_post(row["id"], draft.tweets, meta)
        return f"#{row['id']} {choice['formato']}/{choice['pilar']}"

    # ------------------------------------------------------------------ 5. publicação
    def _publish_due(self):
        now = self.now()
        tol = timedelta(minutes=self.cfg.tolerancia_atraso_min)
        today = self.local().date().isoformat()
        posted_today = self.db.one("SELECT COUNT(*) c FROM posts WHERE status='posted' AND plan_date=?", today)["c"]
        out = []
        for row in self.db.q(f"SELECT * FROM posts WHERE status IN {PENDING_POST} AND scheduled_at<=? ORDER BY scheduled_at", iso(now)):
            late = now - parse_iso(row["scheduled_at"])
            if row["status"] in ("planned", "awaiting_approval"):
                if late > tol:
                    st = "expired" if row["status"] == "awaiting_approval" else "skipped"
                    why = "sem aprovação a tempo" if st == "expired" else "rascunho não ficou pronto a tempo"
                    self.db.update_post(row["id"], status=st, error=why)
                    out.append(f"#{row['id']} {why}")
                continue
            if late > tol:
                self.db.update_post(row["id"], status="skipped", error="o ciclo atrasou além da tolerância")
                out.append(f"#{row['id']} pulado (atraso)")
                continue
            if posted_today >= self.cfg.max_posts_dia_absoluto:
                self.db.update_post(row["id"], status="skipped", error="limite diário absoluto")
                continue
            out.append(self._publish(row))
            posted_today += 1
        return out

    def _publish(self, row) -> str:
        tweets = json.loads(row["tweets"])
        ids: list[str] = []
        try:
            for t in tweets:
                ids.append(self.x.create_post(t, reply_to=ids[-1] if ids else None))
        except XError as e:
            if not ids:
                if e.status == 429:
                    return f"#{row['id']} aguardando (limite de requisições)"
                self.db.update_post(row["id"], status="failed", error=str(e))
                self.log("erro", f"post #{row['id']} falhou: {e}")
                return f"#{row['id']} falhou"
            self.log("aviso", f"thread #{row['id']} publicada parcialmente ({len(ids)}/{len(tweets)}): {e}")
        now = self.now()
        self.db.update_post(
            row["id"], status="posted", tweet_id=ids[0], thread_ids=ids, posted_at=iso(now),
            posted_hour=self.local(now).hour, followers_at_post=self.db.latest_followers(),
        )
        self.log("info", f"publicado #{row['id']} ({row['format']}/{row['pillar']}) -> {ids[0]}")
        return f"#{row['id']} publicado"

    # ------------------------------------------------------------------ 6. métricas
    def _collect_metrics(self):
        now = self.now()
        last = self.db.get("metrics_at")
        if last and now - parse_iso(last) < timedelta(minutes=25):
            return "ok (recente)"
        since = now - timedelta(hours=self.cfg.horizonte_horas + 30)
        rows = self.db.q("SELECT id, tweet_id, posted_at FROM posts WHERE status='posted' AND posted_at>=?", iso(since))
        self.db.set("metrics_at", iso(now))
        if not rows:
            return "nada para medir"
        data = {d["id"]: d for d in self.x.own_posts(self.me()["id"], since)}
        n = 0
        for r in rows:
            d = data.get(r["tweet_id"])
            if not d:
                continue
            age = (now - parse_iso(r["posted_at"])).total_seconds() / 3600
            self.db.x(
                "INSERT INTO metrics(post_id, fetched_at, age_hours, impressions, likes, replies, reposts, quotes, "
                "bookmarks, profile_clicks, url_clicks) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                r["id"], iso(now), age, d.get("impressions", 0), d.get("likes", 0), d.get("replies", 0),
                d.get("reposts", 0), d.get("quotes", 0), d.get("bookmarks", 0), d.get("profile_clicks", 0),
                d.get("url_clicks", 0),
            )
            n += 1
        return f"{n} posts medidos"

    # ------------------------------------------------------------------ 7. respostas
    def _handle_mentions(self):
        if self.cfg.modo_respostas == "off":
            return "desligado"
        me = self.me()
        since_id = self.db.get("mentions_since_id")
        items = self.x.mentions(me["id"], since_id)
        if not self.db.get("mentions_init"):
            # primeira execução: não responde a nada anterior ao início do robô
            if items:
                self.db.set("mentions_since_id", max(items, key=lambda m: int(m["id"]))["id"])
            self.db.set("mentions_init", True)
            return "inicializado"
        for m in sorted(items, key=lambda m: int(m["id"])):
            self.db.x(
                "INSERT OR IGNORE INTO mentions(tweet_id, author_id, author_username, text, created_at, "
                "in_reply_to_tweet_id, mentioned_ids, status) VALUES(?,?,?,?,?,?,?, 'new')",
                m["id"], m["author_id"], m["username"], m["text"], m["created_at"], m["in_reply_to"],
                json.dumps(m.get("mentioned_ids") or []),
            )
        if items:
            newest = max(items, key=lambda m: int(m["id"]))["id"]
            if since_id is None or int(newest) > int(since_id):
                self.db.set("mentions_since_id", newest)
        results = []
        for m in self.db.q("SELECT * FROM mentions WHERE status='new' ORDER BY CAST(tweet_id AS INTEGER)"):
            results.append(self._handle_one(m, me))
        return results

    def _handled_today(self, author_id: str | None = None) -> int:
        start = datetime.combine(self.local().date(), time(0, 0), tzinfo=self.tz)
        sql = ("SELECT COUNT(*) c FROM mentions WHERE status IN ('replied','approved','awaiting_approval') "
               "AND handled_at>=?")
        args = [iso(start)]
        if author_id:
            sql += " AND author_id=?"
            args.append(author_id)
        return self.db.one(sql, *args)["c"]

    def _handle_one(self, m, me) -> str:
        tid = m["tweet_id"]
        now = self.now()

        def skip(reason: str, status: str = "skipped") -> str:
            self.db.update_mention(tid, status=status, reason=reason, handled_at=iso(now))
            return f"{tid}: {reason}"

        if m["author_id"] == me["id"]:
            return skip("post do próprio perfil")
        if self.db.one("SELECT 1 FROM optouts WHERE user_id=?", m["author_id"]):
            return skip("usuário pediu para não receber respostas")
        if matches_opt_out(m["text"], self.cfg.opt_out):
            self.db.x("INSERT OR IGNORE INTO optouts(user_id, username, at) VALUES(?,?,?)",
                      m["author_id"], m["author_username"], iso(now))
            return skip("pedido de opt-out registrado", "optout")
        if m["created_at"] and now - parse_iso(m["created_at"]) > timedelta(hours=24):
            return skip("menção antiga (> 24h)")
        if self._handled_today() >= self.cfg.max_respostas_dia:
            return skip("limite diário de respostas")
        if self._handled_today(m["author_id"]) >= self.cfg.max_respostas_usuario_dia:
            return skip("limite diário por usuário")

        context = None
        if m["in_reply_to_tweet_id"]:
            p = self.db.post_by_tweet(m["in_reply_to_tweet_id"])
            if p and p["tweets"]:
                context = " / ".join(json.loads(p["tweets"]))
        try:
            dec = self.writer.write_reply(ReplyBrief(autor=m["author_username"] or "usuario",
                                                     mensagem=m["text"], post_original=context,
                                                     fatos=self.fatos_vigentes()))
        except Exception as e:
            return f"{tid}: erro ao escrever ({e}); tenta no próximo ciclo"
        if not dec.responder:
            return skip(f"IA optou por não responder: {dec.motivo}")
        probs = problems_for_reply(dec.texto)
        if probs:
            return skip("resposta rejeitada: " + "; ".join(probs))

        self.db.update_mention(tid, reply_text=dec.texto, handled_at=iso(now))
        if self.cfg.modo_respostas == "auto" and self.cfg.aprovacao_x_respostas_ia:
            row = self.db.one("SELECT * FROM mentions WHERE tweet_id=?", tid)
            return self._post_reply(row, dec.texto)
        self.db.update_mention(tid, status="awaiting_approval")
        if self.notifier.enabled:
            self.notifier.ask_reply(tid, m["author_username"], m["text"], dec.texto, context)
        return f"{tid}: aguardando aprovação"

    def _post_reply(self, m, text: str) -> str:
        me = self.me()
        mentioned = json.loads(m["mentioned_ids"] or "[]")
        exclude = [u for u in mentioned if u not in (m["author_id"], me["id"])]
        try:
            rid = self.x.create_post(text, reply_to=m["tweet_id"], exclude_reply_user_ids=exclude or None)
        except XError as e:
            if e.status == 429:
                return f"{m['tweet_id']}: aguardando (limite de requisições)"
            self.db.update_mention(m["tweet_id"], status="failed", reason=str(e))
            return f"{m['tweet_id']}: falhou ({e})"
        self.db.update_mention(m["tweet_id"], status="replied", reply_tweet_id=rid, handled_at=iso(self.now()))
        return f"{m['tweet_id']}: respondido"

    # ------------------------------------------------------------------ 8. relatório
    def _daily_report(self):
        from .report import build_report, daily_summary

        now = self.now()
        last = self.db.get("report_written_at")
        if self.report_path and (not last or now - parse_iso(last) >= timedelta(hours=1)):
            Path(self.report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.report_path).write_text(build_report(self.db, self.cfg, self.model(), now), encoding="utf-8")
            self.db.set("report_written_at", iso(now))
        yesterday = (self.local().date() - timedelta(days=1)).isoformat()
        if self.db.get(f"report_sent:{yesterday}") or self.db.get(f"plan:{yesterday}") is None:
            return "ok"
        self.db.set(f"report_sent:{yesterday}", True)
        if self.notifier.enabled:
            self.notifier.send(daily_summary(self.db, self.cfg, self.model(), yesterday, now))
        return f"resumo de {yesterday} enviado"

    # ------------------------------------------------------------------ consultas para a CLI
    def pending_approvals(self) -> dict:
        return {
            "posts": self.db.q("SELECT * FROM posts WHERE status='awaiting_approval' ORDER BY scheduled_at"),
            "respostas": self.db.q("SELECT * FROM mentions WHERE status='awaiting_approval' ORDER BY tweet_id"),
        }
