"""Otimizador: regressão linear bayesiana + Thompson Sampling.

Cada post é descrito por "braços" em várias dimensões (pilar, formato, tom, gancho, hora).
O modelo estima o efeito aditivo de cada braço sobre a recompensa (views + engajamento em
escala log), controlando o crescimento da conta (log de seguidores) e fim de semana.

Por que não um teste A/B clássico? Porque aqui testamos ~40 variantes ao mesmo tempo e
queremos *ganhar* enquanto testamos: Thompson Sampling aloca mais posts para o que está
indo bem, sem parar de testar o resto. Observações antigas perdem peso (desconto diário),
então o sistema se readapta quando o público muda.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np


@dataclass
class Observation:
    choice: dict[str, str]       # dimensão -> valor (ex.: {"formato": "lista", "hora": "20"})
    controls: dict[str, float]   # covariáveis que não escolhemos (seguidores, fim de semana)
    reward: float
    age_days: float              # idade da observação (para o desconto)


class FeatureSpace:
    def __init__(self, dims: dict[str, list[str]], controls: list[str]):
        self.dims = {d: list(v) for d, v in dims.items()}
        self.controls = list(controls)
        self.index: dict[tuple[str, str], int] = {}
        i = 1  # 0 = intercepto
        for d, vals in self.dims.items():
            for v in vals:
                self.index[(d, v)] = i
                i += 1
        self.ctrl_index: dict[str, int] = {}
        for c in self.controls:
            self.ctrl_index[c] = i
            i += 1
        self.size = i

    def encode(self, choice: dict[str, str], controls: dict[str, float]) -> np.ndarray:
        x = np.zeros(self.size)
        x[0] = 1.0
        for d, v in choice.items():
            j = self.index.get((d, str(v)))
            if j is not None:
                x[j] = 1.0
        for c, val in controls.items():
            j = self.ctrl_index.get(c)
            if j is not None:
                x[j] = float(val)
        return x


class LinearThompson:
    """Regressão bayesiana conjugada (Normal-Inversa-Gama simplificada) com pesos de desconto.

    Priori: w ~ N(0, σ² Λ0⁻¹). arm_sd é o desvio a priori de um efeito *em unidades de ruído*.
    """

    def __init__(
        self,
        space: FeatureSpace,
        arm_sd: float = 0.7,
        control_sd: float = 2.0,
        intercept_sd: float = 10.0,
        discount: float = 0.97,
        noise_sd_prior: float = 1.0,
        noise_prior_n: float = 4.0,
        sample_scale: float = 0.35,
        seed: int | None = None,
    ):
        self.space = space
        self.discount = discount
        self.noise_sd_prior = noise_sd_prior
        self.noise_prior_n = noise_prior_n
        self.sample_scale = sample_scale
        self.np_rng = np.random.default_rng(seed)
        diag = np.empty(space.size)
        diag[0] = 1.0 / intercept_sd**2
        for j in space.index.values():
            diag[j] = 1.0 / arm_sd**2
        for j in space.ctrl_index.values():
            diag[j] = 1.0 / control_sd**2
        self.prior_precision = np.diag(diag)
        self.ybar = 0.0
        self.mu = np.zeros(space.size)
        self.sigma2 = noise_sd_prior**2
        self.cov = self.sigma2 * np.linalg.inv(self.prior_precision)
        self.n_obs = 0
        self.n_eff = 0.0
        self.counts: dict[tuple[str, str], float] = {}

    def fit(self, observations: list[Observation]) -> "LinearThompson":
        self.n_obs = len(observations)
        self.counts = {}
        if not observations:
            return self
        X = np.vstack([self.space.encode(o.choice, o.controls) for o in observations])
        y = np.array([o.reward for o in observations], dtype=float)
        ages = np.array([max(o.age_days, 0.0) for o in observations])
        self.fit_arrays(X, y, ages)
        return self.compute_counts(observations)

    def compute_counts(self, observations: list[Observation]) -> "LinearThompson":
        """Quantos posts (ponderados pelo desconto) sustentam cada braço — usado no relatório."""
        self.counts = {}
        for o in observations:
            wi = self.discount ** max(o.age_days, 0.0)
            for d, v in o.choice.items():
                key = (d, str(v))
                self.counts[key] = self.counts.get(key, 0.0) + float(wi)
        return self

    def fit_arrays(self, X: np.ndarray, y: np.ndarray, ages: np.ndarray) -> "LinearThompson":
        """Ajuste com a matriz de features já pronta (usado também pela simulação, por velocidade)."""
        self.n_obs = len(y)
        if len(y) == 0:
            return self
        w = self.discount ** np.asarray(ages, dtype=float)
        self.ybar = float(np.sum(w * y) / np.sum(w))
        yc = y - self.ybar
        P = self.prior_precision + X.T @ (w[:, None] * X)
        b = X.T @ (w * yc)
        P_inv = np.linalg.inv(P)
        mu = P_inv @ b
        resid = yc - X @ mu
        n_eff = float(np.sum(w))
        sigma2 = (float(np.sum(w * resid**2)) + self.noise_prior_n * self.noise_sd_prior**2) / (n_eff + self.noise_prior_n)
        self.mu, self.sigma2, self.cov, self.n_eff = mu, sigma2, sigma2 * P_inv, n_eff
        return self

    def sample(self) -> np.ndarray:
        cov = (self.cov + self.cov.T) / 2
        try:
            L = np.linalg.cholesky(cov + 1e-9 * np.eye(len(cov)))
        except np.linalg.LinAlgError:
            vals, vecs = np.linalg.eigh(cov)
            L = vecs @ np.diag(np.sqrt(np.clip(vals, 0, None)))
        z = self.np_rng.standard_normal(len(self.mu))
        return self.mu + self.sample_scale * (L @ z)

    # ---- leitura do modelo ----------------------------------------------
    def score(self, w: np.ndarray, dim: str, value: str) -> float:
        j = self.space.index.get((dim, str(value)))
        return float(w[j]) if j is not None else 0.0

    def effects(self, dim: str) -> list[dict]:
        """Efeito estimado de cada valor da dimensão, centrado na média da dimensão."""
        vals = self.space.dims[dim]
        idx = [self.space.index[(dim, v)] for v in vals]
        means = self.mu[idx]
        center = float(np.mean(means))
        out = []
        for v, j, m in zip(vals, idx, means):
            sd = math.sqrt(max(self.cov[j, j], 0.0))
            out.append({"valor": v, "efeito": float(m - center), "incerteza": sd, "n": self.counts.get((dim, v), 0.0)})
        out.sort(key=lambda r: r["efeito"], reverse=True)
        return out

    def prob_best(self, dim: str, draws: int = 400) -> dict[str, float]:
        vals = self.space.dims[dim]
        idx = [self.space.index[(dim, v)] for v in vals]
        wins = {v: 0 for v in vals}
        for _ in range(draws):
            w = self.sample()
            best = vals[int(np.argmax(w[idx]))]
            wins[best] += 1
        return {v: c / draws for v, c in wins.items()}


# ---------------------------------------------------------------------------
# Políticas de escolha (usadas pelo motor real e pela simulação)
# ---------------------------------------------------------------------------
def choose_hours(
    model: LinearThompson,
    allowed: list[int],
    n: int,
    min_gap: int,
    explore: float,
    rng: random.Random,
) -> list[tuple[int, bool]]:
    """Escolhe n horas respeitando o espaçamento mínimo. Retorna [(hora, foi_exploracao)]."""
    w = model.sample()
    scores = {h: model.score(w, "hora", str(h)) for h in allowed}
    chosen: list[tuple[int, bool]] = []
    for _ in range(n):
        pool = [h for h in allowed if all(abs(h - c) >= min_gap for c, _ in chosen)]
        if not pool:
            break
        if rng.random() < explore:
            chosen.append((rng.choice(pool), True))
        else:
            best = max(pool, key=lambda h: (scores[h], -h))
            chosen.append((best, False))
    return sorted(chosen)


def choose_content(
    model: LinearThompson,
    dims: dict[str, list[str]],
    explore: float,
    rng: random.Random,
    avoid: list[dict[str, str]] | None = None,
) -> tuple[dict[str, str], bool]:
    """Escolhe um valor por dimensão de conteúdo (exceto hora) via Thompson + exploração forçada.

    `avoid`: combinações (pilar, formato) já usadas hoje — evita repetir o mesmo par no dia.
    """
    avoid = avoid or []
    used_pairs = {(a.get("pilar"), a.get("formato")) for a in avoid}
    for attempt in range(6):
        w = model.sample()
        choice: dict[str, str] = {}
        explored = False
        for d, vals in dims.items():
            if d == "hora" or not vals:
                continue
            if rng.random() < explore:
                choice[d] = rng.choice(vals)
                explored = True
            else:
                choice[d] = max(vals, key=lambda v: model.score(w, d, v))
        if (choice.get("pilar"), choice.get("formato")) not in used_pairs or attempt == 5:
            return choice, explored
    return choice, explored  # pragma: no cover


def fit_adaptive(space: FeatureSpace, X: np.ndarray, y: np.ndarray, ages: np.ndarray,
                 base: float = 0.97, fast: float = 0.93, holdout: int = 24, margin: float = 0.92,
                 seed: int | None = None, **kw) -> tuple[LinearThompson, float]:
    """Detecta mudança de hábito do público e encurta a memória só quando há evidência.

    Ajusta o modelo sem os `holdout` posts mais novos com memória normal (`base`) e curta (`fast`)
    e compara quem previu melhor esses posts. A memória curta só é adotada se errar pelo menos
    (1 - margin) a menos — assim, com público estável, nada muda.
    """
    chosen = base
    if len(y) >= holdout + 20:
        order = np.argsort(ages)
        test, train = order[:holdout], order[holdout:]
        shift = ages[test].max()
        errs = {}
        for d in (base, fast):
            m = LinearThompson(space, discount=d, **kw).fit_arrays(X[train], y[train], ages[train] - shift)
            errs[d] = float(np.mean((y[test] - (X[test] @ m.mu + m.ybar)) ** 2))
        if errs[fast] < margin * errs[base]:
            chosen = fast
    model = LinearThompson(space, discount=chosen, seed=seed, **kw).fit_arrays(X, y, ages)
    return model, chosen
