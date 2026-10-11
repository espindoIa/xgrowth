"""Linha de comando.

  python -m xgrowth verificar        confere config, chaves e conexão com o X
  python -m xgrowth tick             roda um ciclo (é isso que o agendador chama)
  python -m xgrowth daemon           roda em loop (PC ou servidor ligado 24h)
  python -m xgrowth plano            mostra o plano de hoje
  python -m xgrowth revisar          aprova/descarta pendentes pelo terminal
  python -m xgrowth relatorio        imprime o relatório do que o otimizador aprendeu
  python -m xgrowth simular          roda a simulação de público e compara estratégias
  python -m xgrowth telegram-id      descobre o seu TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from .config import ConfigError, load_config, load_dotenv, secrets_from_env
from .db import DB, parse_iso


def _build(args, need_writer: bool = True):
    from .engine import Engine
    from .notifier import NullNotifier, TelegramNotifier
    from .writer import ClaudeWriter
    from .x_client import DryRunXClient, XClient

    cfg = load_config(args.config)
    sec = secrets_from_env()
    if cfg.dry_run:
        db_path = cfg.db_path.replace(".db", "-dryrun.db")
        db = DB(db_path)
        x = DryRunXClient()
        print(f"[modo dry-run] nada será publicado no X; estado em {db_path}")
    else:
        if not sec.has_x:
            raise ConfigError("Faltam X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET (.env ou secrets).")
        db = DB(cfg.db_path)
        x = XClient(sec.x_api_key, sec.x_api_secret, sec.x_access_token, sec.x_access_secret)
    writer = None
    if need_writer:
        if not sec.anthropic_api_key:
            raise ConfigError("Falta ANTHROPIC_API_KEY (.env ou secrets).")
        writer = ClaudeWriter(cfg, sec.anthropic_api_key)
    notifier = TelegramNotifier(sec.telegram_bot_token, sec.telegram_chat_id, db) if sec.has_telegram else NullNotifier()
    return cfg, db, Engine(cfg, db, x, writer, notifier)


def cmd_verificar(args):
    cfg = load_config(args.config)
    sec = secrets_from_env()
    print(f"config ok: nicho='{cfg.nicho}', {len(cfg.pilares)} pilares, {cfg.posts_por_dia} posts/dia, "
          f"janela {cfg.hora_inicio}h-{cfg.hora_fim}h, posts={cfg.modo_posts}, respostas={cfg.modo_respostas}, "
          f"dry_run={cfg.dry_run}")
    print(f"chaves: X={'ok' if sec.has_x else 'FALTANDO'}  Anthropic={'ok' if sec.anthropic_api_key else 'FALTANDO'}  "
          f"Telegram={'ok' if sec.has_telegram else 'não configurado'}")
    if (cfg.modo_posts == "aprovar" or cfg.modo_respostas == "aprovar") and not sec.has_telegram:
        print("aviso: há itens que precisam de aprovação e o Telegram não está configurado — "
              "use `python -m xgrowth revisar` no terminal ou configure o Telegram.")
    if sec.has_x:
        from .x_client import XClient
        me = XClient(sec.x_api_key, sec.x_api_secret, sec.x_access_token, sec.x_access_secret).me()
        print(f"X conectado: @{me['username']} ({me['followers']} seguidores)")
    if sec.has_telegram:
        from .notifier import TelegramNotifier
        TelegramNotifier(sec.telegram_bot_token, sec.telegram_chat_id, DB(":memory:")).send(
            "✅ xgrowth conectado. As aprovações e o resumo diário vão chegar aqui.")
        print("Telegram: mensagem de teste enviada")


def cmd_tick(args):
    _, _, engine = _build(args)
    stats = engine.tick()
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))


def cmd_daemon(args):
    _, _, engine = _build(args)
    print(f"rodando a cada {args.intervalo} min (Ctrl+C para parar)")
    while True:
        try:
            engine.tick()
        except Exception as e:  # nunca morre por um erro isolado
            print(f"[erro] {e}", file=sys.stderr)
        time.sleep(args.intervalo * 60)


def cmd_plano(args):
    cfg, db, engine = _build(args, need_writer=False)
    date = engine.local().date().isoformat()
    rows = db.q("SELECT * FROM posts WHERE plan_date=? ORDER BY scheduled_at", date)
    if not rows:
        print("Ainda não há plano para hoje (ele é criado no primeiro ciclo do dia).")
    for r in rows:
        when = engine.local(parse_iso(r["scheduled_at"]))
        tag = " (horário em teste)" if r["hour_explored"] else ""
        what = f"{r['format']}/{r['pillar']}/{r['tone']}" if r["format"] else "conteúdo ainda não escolhido"
        print(f"{when:%H:%M}{tag}  [{r['status']}]  {what}")
        if r["tweets"]:
            for t in json.loads(r["tweets"]):
                print(f"      {t}")


def cmd_revisar(args):
    _, db, engine = _build(args, need_writer=False)
    pend = engine.pending_approvals()
    if not pend["posts"] and not pend["respostas"]:
        print("Nada pendente.")
    for p in pend["posts"]:
        when = engine.local(parse_iso(p["scheduled_at"]))
        print(f"\nPost #{p['id']} para {when:%d/%m %H:%M} — {p['format']}/{p['pillar']}/{p['tone']}")
        for t in json.loads(p["tweets"]):
            print(f"  {t}")
        ans = input("Publicar? [s/n/enter=pular] ").strip().lower()
        if ans in ("s", "n"):
            print(engine.decide("p", str(p["id"]), "ok" if ans == "s" else "no"))
    for m in pend["respostas"]:
        print(f"\n@{m['author_username']}: {m['text']}\n  -> {m['reply_text']}")
        ans = input("Responder? [s/n/enter=pular] ").strip().lower()
        if ans in ("s", "n"):
            print(engine.decide("r", m["tweet_id"], "ok" if ans == "s" else "no"))
    print("\nAs respostas aprovadas são enviadas no próximo ciclo (tick).")


def cmd_relatorio(args):
    from .learning import build_model
    from .report import build_report

    cfg, db, engine = _build(args, need_writer=False)
    now = datetime.now(timezone.utc)
    print(build_report(db, cfg, build_model(db, cfg, now), now))


def cmd_simular(args):
    from .sim import SimParams, compare, format_comparison

    windows = ((0, 30), (args.dias - 30, args.dias))
    p = SimParams(days=args.dias, posts_per_day=args.posts_por_dia)
    print(f"Simulando {args.sementes} públicos sintéticos, {args.dias} dias, {args.posts_por_dia} posts/dia...")
    print("Cenário: público estável")
    print(format_comparison(compare(args.sementes, p, windows=windows), windows))
    print("\nCenário: o público muda de hábitos no meio do período")
    print(format_comparison(compare(args.sementes, p, shift_day=args.dias // 2, windows=windows), windows))
    print("\n'captura' = quanto do ganho máximo possível (oráculo vs. sorteio) a estratégia obteve.")


def cmd_telegram_id(args):
    import requests

    load_dotenv(args.env)
    token = secrets_from_env().telegram_bot_token
    if not token:
        raise ConfigError("Defina TELEGRAM_BOT_TOKEN no .env (crie o bot com o @BotFather).")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    chats = {(u.get("message") or {}).get("chat", {}).get("id") for u in r.get("result", [])}
    chats.discard(None)
    if not chats:
        print("Nenhuma mensagem encontrada. Mande /start para o seu bot no Telegram e rode de novo.")
    for c in chats:
        print(f"TELEGRAM_CHAT_ID={c}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xgrowth", description="Perfil no X com otimização automática.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verificar").set_defaults(fn=cmd_verificar)
    sub.add_parser("tick").set_defaults(fn=cmd_tick)
    d = sub.add_parser("daemon")
    d.add_argument("--intervalo", type=int, default=10, help="minutos entre ciclos")
    d.set_defaults(fn=cmd_daemon)
    sub.add_parser("plano").set_defaults(fn=cmd_plano)
    sub.add_parser("revisar").set_defaults(fn=cmd_revisar)
    sub.add_parser("relatorio").set_defaults(fn=cmd_relatorio)
    s = sub.add_parser("simular")
    s.add_argument("--dias", type=int, default=90)
    s.add_argument("--posts-por-dia", type=int, default=4)
    s.add_argument("--sementes", type=int, default=12)
    s.set_defaults(fn=cmd_simular)
    sub.add_parser("telegram-id").set_defaults(fn=cmd_telegram_id)
    args = ap.parse_args(argv)
    load_dotenv(args.env)
    try:
        args.fn(args)
    except ConfigError as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
