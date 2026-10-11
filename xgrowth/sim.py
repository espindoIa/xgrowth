"""Simulação de público para testar e calibrar o otimizador sem gastar um centavo de API.

Cria um "mundo" com efeitos verdadeiros escondidos (picos de audiência em horários, formatos
que funcionam mais, etc.) e ruído pesado, como na vida real. Compara estratégias:

  aleatorio        horários e conteúdo sorteados (um A/B "puro" que nunca para de testar)
  horarios_fixos   9h/12h/18h/21h (a dica genérica da internet) + conteúdo sorteado
  otimizador       o Thompson Sampling deste projeto
  oraculo          sabe os efeitos verdadeiros (teto teórico)

Métrica principal: quanto do ganho máximo possível (oráculo vs aleatório) cada estratégia captura.
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field

import numpy as np

from .bandit import FeatureSpace, LinearThompson, choose_content, choose_hours, fit_adaptive

DIM_SIZES = {"pilar": 4, "formato": 7, "tom": 4, "gancho": 4}
DIM_SD = {"pilar": 0.35, "formato": 0.45, "tom": 0.25, "gancho": 0.25}


@dataclass
class World:
    dims: dict[str, list[str]]
    effects: dict[str, dict[str, float]]
    effects_after: dict[str, dict[str, float]] | None = None
    shift_day: int | None = None
    noise_sd: float = 1.0
    base: float = 6.0
    interactions: dict[tuple[str, str], float] | None = None  # efeito extra de (formato, hora): o modelo não enxerga

    def eff(self, day: int):
        if self.shift_day is not None and day >= self.shift_day and self.effects_after:
            return self.effects_after
        return self.effects

    def expected(self, choice: dict[str, str], day: int) -> float:
        e = self.eff(day)
        extra = self.interactions.get((choice["formato"], choice["hora"]), 0.0) if self.interactions else 0.0
        return self.base + sum(e[d][choice[d]] for d in self.dims) + extra

    def draw(self, choice, day, rng: np.random.Generator) -> float:
        return self.expected(choice, day) + rng.normal(0, self.noise_sd)


def _hour_curve(hours, p1, p2, a1, a2, width=1.6):
    raw = {str(h): a1 * math.exp(-((h - p1) ** 2) / (2 * width**2)) + a2 * math.exp(-((h - p2) ** 2) / (2 * width**2))
           for h in hours}
    m = sum(raw.values()) / len(raw)
    return {k: v - m for k, v in raw.items()}


def make_world(seed: int, hours: list[int], shift_day: int | None = None, noise_sd: float = 1.0,
               interaction_sd: float = 0.0) -> World:
    rng = np.random.default_rng(seed)
    dims = {d: [f"{d}{i}" for i in range(n)] for d, n in DIM_SIZES.items()}
    dims["hora"] = [str(h) for h in hours]
    p1, p2 = rng.uniform(11, 14), rng.uniform(18.5, 22)
    a1, a2 = rng.uniform(0.4, 0.9), rng.uniform(0.5, 1.0)
    effects = {d: {v: float(rng.normal(0, DIM_SD[d])) for v in dims[d]} for d in DIM_SIZES}
    effects["hora"] = _hour_curve(hours, p1, p2, a1, a2)
    after = None
    if shift_day is not None:
        after = {d: dict(zip(dims[d], rng.permutation(list(effects[d].values())))) for d in DIM_SIZES}
        after["hora"] = _hour_curve(hours, p1 - 2.0, p2 + 1.5, a2, a1)
    inter = None
    if interaction_sd > 0:
        inter = {(f, h): float(rng.normal(0, interaction_sd)) for f in dims["formato"] for h in dims["hora"]}
    return World(dims=dims, effects=effects, effects_after=after, shift_day=shift_day, noise_sd=noise_sd,
                 interactions=inter)


@dataclass
class SimParams:
    days: int = 90
    posts_per_day: int = 4
    min_gap: int = 2
    hours: list[int] = field(default_factory=lambda: list(range(7, 23)))
    horizon_h: float = 24.0
    explore: float = 0.03
    discount: float | str = "auto"
    arm_sd: float = 0.7
    sample_scale: float = 0.35


def _random_hours(hours, n, gap, rng: random.Random):
    pool = hours[:]
    rng.shuffle(pool)
    chosen = []
    for h in pool:
        if all(abs(h - c) >= gap for c in chosen):
            chosen.append(h)
        if len(chosen) == n:
            break
    return sorted(chosen)


def _oracle_hours(world: World, day: int, hours, n, gap):
    e = world.eff(day)["hora"]
    best, best_v = None, -1e9
    for combo in itertools.combinations(hours, n):
        if all(b - a >= gap for a, b in zip(combo, combo[1:])):
            v = sum(e[str(h)] for h in combo)
            if v > best_v:
                best, best_v = combo, v
    return list(best)


def run_strategy(world: World, strategy: str, p: SimParams, seed: int) -> dict:
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed + 10_000)
    content_dims = {d: v for d, v in world.dims.items() if d != "hora"}
    space = FeatureSpace(world.dims, [])
    X_rows: list[np.ndarray] = []
    ys: list[float] = []
    times: list[float] = []
    per_day: list[float] = []

    def fitted(t_now: float) -> LinearThompson:
        seed_ = rng.randrange(2**31)
        idx = [i for i, t in enumerate(times) if t + p.horizon_h <= t_now]
        if idx:
            X = np.vstack([X_rows[i] for i in idx])
            y = np.array([ys[i] for i in idx])
            ages = np.array([(t_now - times[i]) / 24 for i in idx])
            if p.discount == "auto":
                return fit_adaptive(space, X, y, ages, seed=seed_, arm_sd=p.arm_sd, sample_scale=p.sample_scale)[0]
            return LinearThompson(space, arm_sd=p.arm_sd, discount=p.discount, sample_scale=p.sample_scale,
                                  seed=seed_).fit_arrays(X, y, ages)
        d = 0.97 if p.discount == "auto" else p.discount
        return LinearThompson(space, arm_sd=p.arm_sd, discount=d, sample_scale=p.sample_scale, seed=seed_)

    for day in range(p.days):
        t0 = day * 24.0
        if strategy == "aleatorio":
            hours = _random_hours(p.hours, p.posts_per_day, p.min_gap, rng)
        elif strategy == "horarios_fixos":
            hours = [9, 12, 18, 21][: p.posts_per_day]
        elif strategy == "oraculo":
            hours = _oracle_hours(world, day, p.hours, p.posts_per_day, p.min_gap)
        else:
            hours = [h for h, _ in choose_hours(fitted(t0), p.hours, p.posts_per_day, p.min_gap, p.explore, rng)]
        exp_vals = []
        for h in hours:
            t = t0 + h
            if strategy in ("aleatorio", "horarios_fixos"):
                choice = {d: rng.choice(v) for d, v in content_dims.items()}
            elif strategy == "oraculo":
                e = world.eff(day)
                choice = {d: max(v, key=lambda x: e[d][x]) for d, v in content_dims.items()}
            else:
                choice, _ = choose_content(fitted(t - 1), content_dims, p.explore, rng)
            choice["hora"] = str(h)
            ys.append(world.draw(choice, day, nrng))
            X_rows.append(space.encode(choice, {}))
            times.append(t)
            exp_vals.append(world.expected(choice, day))
        per_day.append(float(np.mean(exp_vals)))
    return {"per_day": per_day}


def compare(n_seeds: int = 12, p: SimParams | None = None, shift_day: int | None = None,
            noise_sd: float = 1.0, interaction_sd: float = 0.0,
            strategies=("aleatorio", "horarios_fixos", "otimizador", "oraculo"), windows=((0, 30), (60, 90))) -> dict:
    p = p or SimParams()
    res = {s: {w: [] for w in windows} for s in strategies}
    for seed in range(n_seeds):
        world = make_world(seed, p.hours, shift_day=shift_day, noise_sd=noise_sd, interaction_sd=interaction_sd)
        for s in strategies:
            per_day = run_strategy(world, s, p, seed * 7 + 1)["per_day"]
            for a, b in windows:
                res[s][(a, b)].append(float(np.mean(per_day[a:b])))
    out = {}
    for w in windows:
        R = np.array(res["aleatorio"][w])
        O = np.array(res["oraculo"][w])
        for s in strategies:
            S = np.array(res[s][w])
            cap = (S - R) / np.maximum(O - R, 1e-9)
            out.setdefault(s, {})[w] = {"media": float(S.mean()), "captura": float(cap.mean()),
                                       "captura_p10": float(np.percentile(cap, 10))}
    return out


def format_comparison(out: dict, windows) -> str:
    lines = []
    head = "estratégia".ljust(16) + "".join(f"  dias {a+1}-{b}: captura".ljust(26) for a, b in windows)
    lines.append(head)
    for s, d in out.items():
        row = s.ljust(16)
        for w in windows:
            row += f"  {d[w]['captura']*100:6.1f}% (p10 {d[w]['captura_p10']*100:5.1f}%)".ljust(26)
        lines.append(row)
    return "\n".join(lines)
