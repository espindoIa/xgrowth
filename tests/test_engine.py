import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from xgrowth.db import parse_iso
from xgrowth.learning import build_model
from xgrowth.notifier import TelegramNotifier
from xgrowth.report import build_report

from conftest import FakeWriter

TZ = ZoneInfo("America/Sao_Paulo")


def run_ticks(eng, clock, hours, step_min=30):
    for _ in range(int(hours * 60 / step_min)):
        eng.tick()
        clock.advance(minutes=step_min)


def posted(eng):
    return eng.db.q("SELECT * FROM posts WHERE status='posted' ORDER BY posted_at")


# ---------------------------------------------------------------- um dia completo
def test_full_day_publishes_planned_posts_in_window(make_engine, clock, cfg):
    eng = make_engine()
    run_ticks(eng, clock, 24)
    rows = posted(eng)
    assert len(rows) == cfg.posts_por_dia
    hours = [r["posted_hour"] for r in rows]
    assert all(cfg.hora_inicio <= h < cfg.hora_fim for h in hours)
    assert all(b - a >= cfg.espacamento_min_horas for a, b in zip(hours, hours[1:]))
    for r in rows:
        assert r["posted_hour"] == r["hour"]  # publicou na hora planejada
        late = parse_iso(r["posted_at"]) - parse_iso(r["scheduled_at"])
        assert timedelta(0) <= late <= timedelta(minutes=30)
        assert r["pillar"] in cfg.pilares and r["format"] in cfg.formatos
    # threads saem encadeadas
    for p in eng.x.published:
        if p["reply_to"]:
            assert any(q["id"] == p["reply_to"] for q in eng.x.published)
    # nenhum par (pilar, formato) repetido no mesmo dia
    pairs = [(r["pillar"], r["format"]) for r in rows]
    assert len(set(pairs)) == len(pairs)


def test_bad_drafts_are_retried_with_feedback(make_engine, clock):
    w = FakeWriter(bad_first=2)
    eng = make_engine(writer=w)
    run_ticks(eng, clock, 24)
    assert len(posted(eng)) == 4
    assert w.post_calls[1].feedback and any("link" in f for f in w.post_calls[1].feedback)
    assert all("https" not in p["text"] for p in eng.x.published)


def test_pause_does_nothing(make_engine, clock):
    eng = make_engine(pausado=True)
    run_ticks(eng, clock, 24)
    assert eng.x.published == []


def test_missed_slot_is_skipped_not_posted_late(make_engine, clock):
    eng = make_engine()
    eng.tick()  # planeja
    first = eng.db.one("SELECT * FROM posts ORDER BY scheduled_at LIMIT 1")
    clock.t = parse_iso(first["scheduled_at"]) + timedelta(hours=3)  # o agendador ficou fora do ar
    eng.tick()
    assert eng.db.one("SELECT status FROM posts WHERE id=?", first["id"])["status"] == "skipped"


# ---------------------------------------------------------------- ciclo de aprendizado
def test_learning_loop_collects_metrics_and_learns(make_engine, clock, cfg):
    eng = make_engine()
    x = eng.x
    # público sintético: listas e 20h performam muito melhor
    def metrics_for(post_row):
        boost = (3 if post_row["format"] == "lista" else 1) * (3 if post_row["posted_hour"] in (19, 20, 21) else 1)
        return {"impressions": 100 * boost, "likes": 5 * boost, "replies": boost, "reposts": boost // 2,
                "quotes": 0, "bookmarks": boost, "profile_clicks": boost, "url_clicks": 0}
    for _day in range(14):
        for _ in range(48):
            eng.tick()
            for r in posted(eng):
                x.fake_metrics[r["tweet_id"]] = metrics_for(r)
            clock.advance(minutes=30)
    n_posts = len(posted(eng))
    assert n_posts == 14 * cfg.posts_por_dia
    snaps = eng.db.one("SELECT COUNT(DISTINCT post_id) c FROM metrics WHERE age_hours>=24")["c"]
    assert snaps >= n_posts - cfg.posts_por_dia - 1
    model = build_model(eng.db, cfg, clock())
    assert model.n_obs >= n_posts - cfg.posts_por_dia - 1
    assert model.effects("formato")[0]["valor"] == "lista"
    top_hours = [e["valor"] for e in model.effects("hora")[:4]]
    assert any(h in top_hours for h in ("19", "20", "21"))
    # e passa a usar mais o que funciona
    last_week = posted(eng)[-7 * cfg.posts_por_dia:]
    assert sum(r["format"] == "lista" for r in last_week) >= 7
    report = build_report(eng.db, cfg, model, clock())
    assert "O que o otimizador aprendeu" in report and "lista" in report


# ---------------------------------------------------------------- respostas
def _mention(i, author="42", user="ana", text="Boa dica, funcionou aqui!", reply_to=None, clock=None, mentioned=None):
    return {"id": str(i), "author_id": author, "username": user, "text": text,
            "created_at": clock().isoformat() if clock else None, "in_reply_to": reply_to,
            "mentioned_ids": mentioned or []}


def test_replies_need_approval_and_respect_rules(make_engine, clock):
    eng = make_engine()
    run_ticks(eng, clock, 12)  # publica posts da manhã e inicializa as menções
    our = posted(eng)[0]
    x = eng.x
    x.fake_mentions = [
        _mention(5001, text="@perfil_teste boa! qual cooler você recomenda?", reply_to=our["tweet_id"], clock=clock,
                 mentioned=["999", "777"]),
        _mention(5002, author="43", user="spammer", text="SPAM compre seguidores", clock=clock),
        _mention(5003, author="44", user="bia", text="@perfil_teste pare de me responder", clock=clock),
        _mention(5004, author="42", user="ana", text="outra pergunta", clock=clock),
        _mention(5005, author="42", user="ana", text="mais uma", clock=clock),
    ]
    eng.tick()
    st = {m["tweet_id"]: m for m in eng.db.q("SELECT * FROM mentions")}
    assert st["5001"]["status"] == "awaiting_approval"
    assert st["5002"]["status"] == "skipped"
    assert st["5003"]["status"] == "optout"
    assert st["5004"]["status"] == "awaiting_approval"
    assert st["5005"]["status"] == "skipped" and "por usuário" in st["5005"]["reason"]
    assert eng.db.one("SELECT 1 FROM optouts WHERE user_id='44'")
    # contexto do post original foi passado para a IA
    assert any(b.post_original for b in eng.writer.reply_calls)
    n_before = len(x.published)
    assert eng.decide("r", "5001", "ok").startswith("resposta a 5001 aprovada")
    eng.decide("r", "5004", "no")
    eng.tick()
    replies = x.published[n_before:]
    replies = [p for p in replies if p["reply_to"] in ("5001", "5004")]
    assert len(replies) == 1 and replies[0]["reply_to"] == "5001"
    assert replies[0]["exclude"] == ["777"]  # não arrasta terceiros para a conversa
    assert eng.db.one("SELECT status FROM mentions WHERE tweet_id='5001'")["status"] == "replied"
    # usuário que pediu opt-out nunca mais recebe resposta
    x.fake_mentions.append(_mention(5006, author="44", user="bia", text="voltei, e aí?", clock=clock))
    eng.tick()
    assert eng.db.one("SELECT status FROM mentions WHERE tweet_id='5006'")["status"] == "skipped"
    # 1 resposta por interação: a mesma menção não é processada duas vezes
    eng.tick()
    assert len([p for p in x.published if p["reply_to"] == "5001"]) == 1


def test_first_run_ignores_old_mentions(make_engine, clock):
    eng = make_engine()
    eng.x.fake_mentions = [_mention(100, clock=clock), _mention(101, clock=clock)]
    eng.tick()
    assert eng.db.q("SELECT * FROM mentions") == []
    eng.x.fake_mentions.append(_mention(102, clock=clock))
    eng.tick()
    assert [m["tweet_id"] for m in eng.db.q("SELECT * FROM mentions")] == ["102"]


def test_auto_replies_only_with_x_approval(make_engine, clock):
    eng = make_engine(modo_respostas="auto", aprovacao_x_respostas_ia=True)
    eng.tick()
    eng.x.fake_mentions = [_mention(9001, clock=clock)]
    eng.tick()
    assert eng.db.one("SELECT status FROM mentions WHERE tweet_id='9001'")["status"] == "replied"


# ---------------------------------------------------------------- aprovação de posts
def test_post_approval_mode(make_engine, clock):
    eng = make_engine(modo_posts="aprovar")
    eng.tick()
    waiting = eng.db.q("SELECT * FROM posts WHERE status='awaiting_approval' ORDER BY scheduled_at")
    assert waiting, "rascunhos devem ser gerados com antecedência para aprovação"
    eng.decide("p", str(waiting[0]["id"]), "ok")
    run_ticks(eng, clock, 24)
    st = {r["id"]: r["status"] for r in eng.db.q("SELECT id, status FROM posts")}
    assert st[waiting[0]["id"]] == "posted"
    assert all(s in ("posted", "expired") for s in st.values())
    assert list(st.values()).count("posted") == 1


class FakeTelegram:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        result = self.updates if method == "getUpdates" else {"message_id": 1}
        return type("R", (), {"json": lambda self_: {"ok": True, "result": result}})()


def test_telegram_accepts_only_configured_chat(make_engine, clock):
    eng = make_engine(modo_posts="aprovar")
    eng.tick()
    pid = str(eng.db.one("SELECT id FROM posts WHERE status='awaiting_approval'")["id"])
    updates = [
        {"update_id": 1, "callback_query": {"id": "a", "data": f"p:{pid}:ok", "message": {"chat": {"id": 666}, "message_id": 5}}},
        {"update_id": 2, "callback_query": {"id": "b", "data": f"p:{pid}:no", "message": {"chat": {"id": 123}, "message_id": 6, "text": "t"}}},
    ]
    tg = TelegramNotifier("TOKEN", "123", eng.db, session=FakeTelegram(updates))
    eng.notifier = tg
    eng.tick()
    assert eng.db.one("SELECT status FROM posts WHERE id=?", int(pid))["status"] == "rejected"
    assert eng.db.get("tg_offset") == 3


def test_report_and_daily_summary_build(make_engine, clock, cfg):
    eng = make_engine()
    run_ticks(eng, clock, 30)
    model = build_model(eng.db, cfg, clock())
    txt = build_report(eng.db, cfg, model, clock())
    assert "Relatório" in txt
    assert eng.db.get("report_sent:2026-10-12") is True


def test_late_start_only_plans_future_slots(make_engine, clock):
    from datetime import datetime, timezone
    clock.t = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)  # 20:30 em São Paulo
    eng = make_engine()
    eng.tick()
    rows = eng.db.q("SELECT * FROM posts")
    assert rows and all(r["hour"] >= 20 for r in rows)
    assert all(parse_iso(r["scheduled_at"]) > clock() for r in rows)
    clock.t = datetime(2026, 10, 13, 2, 55, tzinfo=timezone.utc)  # 23:55: nada mais cabe hoje
    eng2 = make_engine()
    eng2.tick()
    assert eng2.db.q("SELECT * FROM posts") == []


def test_unapproved_replies_expire(make_engine, clock):
    eng = make_engine()
    eng.tick()
    eng.x.fake_mentions = [_mention(7001, clock=clock)]
    eng.tick()
    assert eng.db.one("SELECT status FROM mentions WHERE tweet_id='7001'")["status"] == "awaiting_approval"
    clock.advance(hours=37)
    eng.tick()
    assert eng.db.one("SELECT status FROM mentions WHERE tweet_id='7001'")["status"] == "expired"


def test_invented_score_is_rejected_and_game_day_reaches_prompt(make_engine, clock, cfg):
    from conftest import FakeWriter
    from xgrowth.writer import PostDraft

    class ScoreWriter(FakeWriter):
        def write_post(self, brief):
            d = super().write_post(brief)
            if not brief.feedback:
                return PostDraft(tema="inventado", tweets=["Ganhamos de 3x1 em 1997 com sobra"])
            return d

    w = ScoreWriter()
    eng = make_engine(writer=w, dias_de_jogo={"2026-10-12"}, fatos=[])
    run_ticks(eng, clock, 24)
    assert len(posted(eng)) == 4
    assert all("3x1" not in p["text"] for p in eng.x.published)
    assert any("placar 3x1" in f for b in w.post_calls for f in b.feedback)
    assert all(b.dia_de_jogo for b in w.post_calls)
