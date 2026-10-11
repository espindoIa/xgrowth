"""Relatórios: o que o otimizador aprendeu e como a conta está indo."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .config import Config
from .db import DB, parse_iso
from .learning import compute_reward, mature_metrics

DIM_NAMES = {"hora": "Horário", "formato": "Formato", "pilar": "Pilar", "tom": "Tom", "gancho": "Gancho"}


def _followers_series(db: DB) -> list[tuple[datetime, int]]:
    return [(parse_iso(r["at"]), r["count"]) for r in db.q("SELECT at, count FROM followers ORDER BY at")]


def _followers_at(series, when: datetime) -> int | None:
    best = None
    for at, c in series:
        if at <= when:
            best = c
    return best


def _post_rows(db: DB, cfg: Config, now: datetime, since: datetime | None = None):
    rows = []
    for p in db.q("SELECT * FROM posts WHERE status='posted' ORDER BY posted_at"):
        posted = parse_iso(p["posted_at"])
        if since and posted < since:
            continue
        last = db.one("SELECT * FROM metrics WHERE post_id=? ORDER BY age_hours DESC LIMIT 1", p["id"])
        age_h = (now - posted).total_seconds() / 3600
        mature = mature_metrics(db, p["id"], cfg.horizonte_horas, age_h)
        rows.append({
            "id": p["id"], "posted": posted, "texto": " / ".join(json.loads(p["tweets"] or "[]")),
            "formato": p["format"], "pilar": p["pillar"], "hora": p["posted_hour"],
            "views": last["impressions"] if last else None,
            "curtidas": last["likes"] if last else None,
            "respostas": last["replies"] if last else None,
            "reposts": (last["reposts"] + last["quotes"]) if last else None,
            "cliques_perfil": last["profile_clicks"] if last else None,
            "recompensa": compute_reward(dict(mature), cfg) if mature else None,
        })
    return rows


def _fmt_effect(e: float) -> str:
    arrow = "▲" if e > 0.05 else ("▼" if e < -0.05 else "•")
    return f"{arrow} {e:+.2f}"


def build_report(db: DB, cfg: Config, model, now: datetime) -> str:
    tz = ZoneInfo(cfg.fuso)
    me = db.get("me") or {}
    series = _followers_series(db)
    f_now = series[-1][1] if series else None
    f_7d = _followers_at(series, now - timedelta(days=7))
    lines = [f"# Relatório — @{me.get('username', '?')}", "",
             f"Gerado em {now.astimezone(tz):%d/%m/%Y %H:%M} ({cfg.fuso}).", ""]
    if f_now is not None:
        delta = f" ({f_now - f_7d:+d} em 7 dias)" if f_7d is not None else ""
        lines.append(f"**Seguidores:** {f_now}{delta}")
    rows = _post_rows(db, cfg, now, since=now - timedelta(days=7))
    if rows:
        views = [r["views"] for r in rows if r["views"] is not None]
        lines.append(f"**Posts nos últimos 7 dias:** {len(rows)}"
                     + (f" · mediana de {int(median(views))} views por post" if views else ""))
    lines += ["", f"Observações usadas pelo otimizador: {model.n_obs} posts maduros (≥ {cfg.horizonte_horas:.0f}h). "
              f"Memória: {getattr(model, 'memoria', 'normal')}.", ""]

    if model.n_obs >= 3:
        lines += ["## O que o otimizador aprendeu", "",
                  "Efeito estimado sobre a pontuação (escala log de views + engajamento). "
                  "± é a incerteza; com poucos posts, tudo ainda é provisório.", ""]
        for dim in ["hora", "formato", "pilar", "tom", "gancho"]:
            effs = model.effects(dim)
            lines += [f"### {DIM_NAMES[dim]}", "", "| valor | efeito | ± | posts |", "|---|---|---|---|"]
            for e in effs[: (8 if dim == "hora" else len(effs))]:
                label = f"{e['valor']}h" if dim == "hora" else e["valor"]
                lines.append(f"| {label} | {_fmt_effect(e['efeito'])} | {e['incerteza']:.2f} | {e['n']:.0f} |")
            lines.append("")
    else:
        lines += ["Ainda sem dados suficientes: o otimizador está explorando horários e formatos.", ""]

    all_rows = [r for r in _post_rows(db, cfg, now) if r["recompensa"] is not None]
    if all_rows:
        lines += ["## Melhores posts até agora", ""]
        for r in sorted(all_rows, key=lambda r: r["recompensa"], reverse=True)[:5]:
            lines.append(f"- **{r['views']} views**, {r['curtidas']} curtidas, {r['respostas']} respostas, "
                         f"{r['cliques_perfil']} cliques no perfil — _{r['formato']}/{r['pilar']} às {r['hora']}h_: "
                         f"{r['texto'][:180]}")
        lines.append("")

    pend = db.one("SELECT COUNT(*) c FROM posts WHERE status='awaiting_approval'")["c"]
    pend_r = db.one("SELECT COUNT(*) c FROM mentions WHERE status='awaiting_approval'")["c"]
    if pend or pend_r:
        lines += [f"Pendentes de aprovação: {pend} posts, {pend_r} respostas.", ""]
    errs = db.q("SELECT at, msg FROM log WHERE level IN ('erro','aviso') ORDER BY at DESC LIMIT 5")
    if errs:
        lines += ["## Últimos avisos", ""] + [f"- {e['at']}: {e['msg']}" for e in errs] + [""]
    return "\n".join(lines)


def daily_summary(db: DB, cfg: Config, model, date: str, now: datetime) -> str:
    rows = [r for r in db.q("SELECT * FROM posts WHERE plan_date=?", date)]
    posted = [r for r in rows if r["status"] == "posted"]
    series = _followers_series(db)
    f_now = series[-1][1] if series else None
    tz = ZoneInfo(cfg.fuso)
    day_start = datetime.fromisoformat(date).replace(tzinfo=tz)
    f_then = _followers_at(series, day_start)
    parts = [f"📊 Resumo de {date}", f"Posts publicados: {len(posted)}/{len(rows)}"]
    if f_now is not None and f_then is not None:
        parts.append(f"Seguidores: {f_now} ({f_now - f_then:+d})")
    replied = db.one("SELECT COUNT(*) c FROM mentions WHERE status='replied' AND substr(handled_at,1,10)>=?", date)["c"]
    parts.append(f"Respostas enviadas: {replied}")
    if model.n_obs >= 3:
        for dim in ("hora", "formato"):
            top = model.effects(dim)[0]
            v = f"{top['valor']}h" if dim == "hora" else top["valor"]
            parts.append(f"Melhor {DIM_NAMES[dim].lower()} até agora: {v}")
    return "\n".join(parts)
