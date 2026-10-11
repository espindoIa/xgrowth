"""Transforma métricas em recompensa e o histórico de posts em observações para o otimizador."""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from .bandit import FeatureSpace, LinearThompson, Observation, fit_adaptive
from .config import Config
from .db import DB, parse_iso

CONTROLS = ["log_seguidores", "fim_de_semana"]


def weighted_engagement(m: dict, pesos: dict[str, float]) -> float:
    return sum(float(m.get(k, 0) or 0) * w for k, w in pesos.items())


def compute_reward(m: dict, cfg: Config) -> float:
    """Recompensa em escala log: robusta a posts que viralizam (um outlier não domina o modelo)."""
    imp = float(m.get("impressions", 0) or 0)
    eng = weighted_engagement(m, cfg.pesos_acoes)
    return cfg.peso_views * math.log1p(imp) + cfg.peso_engajamento * math.log1p(eng)


def controls_for(posted_at: datetime, followers: int | None, tz: ZoneInfo) -> dict[str, float]:
    local = posted_at.astimezone(tz)
    return {
        "log_seguidores": math.log1p(max(followers or 0, 0)),
        "fim_de_semana": 1.0 if local.weekday() >= 5 else 0.0,
    }


def mature_metrics(db: DB, post_id: int, horizon_h: float, post_age_h: float):
    """Snapshot mais próximo do horizonte (>= horizonte). Se a coleta falhou nesse período,
    usa o último snapshot com pelo menos 75% do horizonte quando o post já passou de horizonte+24h."""
    r = db.one(
        "SELECT * FROM metrics WHERE post_id=? AND age_hours>=? ORDER BY age_hours ASC LIMIT 1",
        post_id, horizon_h,
    )
    if r:
        return r
    if post_age_h >= horizon_h + 24:
        return db.one(
            "SELECT * FROM metrics WHERE post_id=? AND age_hours>=? ORDER BY age_hours DESC LIMIT 1",
            post_id, 0.75 * horizon_h,
        )
    return None


def build_observations(db: DB, cfg: Config, now: datetime) -> list[Observation]:
    tz = ZoneInfo(cfg.fuso)
    obs: list[Observation] = []
    for p in db.q("SELECT * FROM posts WHERE status='posted' AND posted_at IS NOT NULL"):
        posted_at = parse_iso(p["posted_at"])
        age_h = (now - posted_at).total_seconds() / 3600
        m = mature_metrics(db, p["id"], cfg.horizonte_horas, age_h)
        if m is None:
            continue
        choice = {
            "pilar": p["pillar"], "formato": p["format"], "tom": p["tone"], "gancho": p["hook"],
            "hora": str(p["posted_hour"] if p["posted_hour"] is not None else p["hour"]),
        }
        obs.append(Observation(
            choice={k: v for k, v in choice.items() if v is not None},
            controls=controls_for(posted_at, p["followers_at_post"], tz),
            reward=compute_reward(dict(m), cfg),
            age_days=age_h / 24.0,
        ))
    return obs


def build_model(db: DB, cfg: Config, now: datetime, seed: int | None = None) -> LinearThompson:
    space = FeatureSpace(cfg.dimensoes(), CONTROLS)
    obs = build_observations(db, cfg, now)
    kw = {"sample_scale": cfg.escala_amostragem}
    if cfg.desconto_diario == "auto":
        if obs:
            X = np.vstack([space.encode(o.choice, o.controls) for o in obs])
            y = np.array([o.reward for o in obs])
            ages = np.array([max(o.age_days, 0.0) for o in obs])
            model, d = fit_adaptive(space, X, y, ages, seed=seed, **kw)
            model.memoria = "curta (o público parece ter mudado)" if d < 0.97 else "normal"
            return model.compute_counts(obs)
        model = LinearThompson(space, discount=0.97, seed=seed, **kw)
    else:
        model = LinearThompson(space, discount=float(cfg.desconto_diario), seed=seed, **kw).fit(obs)
    model.memoria = "normal"
    return model
