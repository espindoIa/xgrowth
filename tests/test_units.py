import random

import numpy as np
import pytest

from xgrowth.bandit import FeatureSpace, LinearThompson, Observation, choose_content, choose_hours
from xgrowth.config import ConfigError, config_from_dict
from xgrowth.textutil import (matches_opt_out, problems_for_post, problems_for_reply, similarity,
                              weighted_length)
from xgrowth.writer import post_tool, system_prompt_post, user_prompt_post, PostBrief


# ---------------------------------------------------------------- texto
def test_weighted_length():
    assert weighted_length("a" * 280) == 280
    assert weighted_length("ação") == 4
    assert weighted_length("🔥") == 2
    assert weighted_length("日本") == 4


def test_post_validation_catches_links_mentions_bait_and_length():
    assert problems_for_post(["dica boa e curta"], 1) == []
    assert problems_for_post(["uma lista curta de dicas"], 1) == []  # 'curta' não é engagement bait
    assert any("link" in p for p in problems_for_post(["veja site.com.br"], 1))
    assert any("link" in p for p in problems_for_post(["https://x.com/a"], 1))
    assert any("menção" in p for p in problems_for_post(["fala @fulano"], 1))
    assert any("bait" in p for p in problems_for_post(["dá like se concorda"], 1))
    assert any("bait" in p for p in problems_for_post(["me siga para mais"], 1))
    assert any("caracteres" in p for p in problems_for_post(["x" * 281], 1))
    assert any("hashtags" in p for p in problems_for_post(["#a #b"], 1))
    assert any("thread com 3" in p for p in problems_for_post(["a", "b", "c"], 2))
    assert problems_for_post(["preço R$ 1.500,00 em 2026"], 1) == []  # números não são links


def test_reply_validation():
    assert problems_for_reply("Valeu pelo toque!") == []
    assert problems_for_reply("") == ["resposta vazia"]
    assert problems_for_reply("olha @outro") == ["resposta com @menção"]


def test_opt_out_detection():
    phrases = ["pare", "stop", "pare de me responder", "não me responda"]
    assert matches_opt_out("@perfil pare de me responder", phrases)
    assert matches_opt_out("STOP", phrases)
    assert matches_opt_out("por favor nao me responda mais", phrases)  # sem acento também
    assert not matches_opt_out("comparei as duas placas", phrases)  # 'pare' dentro de outra palavra
    assert not matches_opt_out("parece bom", phrases)


def test_similarity():
    assert similarity("placa de vídeo boa e barata", "placa de vídeo boa e barata") == 1.0
    assert similarity("placa de vídeo", "teclado mecânico silencioso") == 0.0


# ---------------------------------------------------------------- config
BASE = {"perfil": {"nicho": "n"}, "pilares": [{"id": "a", "descricao": "A"}, {"id": "b", "descricao": "B"}]}


def test_config_defaults_and_validation():
    cfg = config_from_dict(dict(BASE))
    assert cfg.modo_respostas == "aprovar" and cfg.dry_run is True
    assert "hora" in cfg.dimensoes() and len(cfg.dimensoes()["hora"]) == 16
    with pytest.raises(ConfigError, match="aprovação prévia"):
        config_from_dict({**BASE, "respostas": {"modo": "auto"}})
    ok = config_from_dict({**BASE, "respostas": {"modo": "auto", "tenho_aprovacao_do_x_para_respostas_ia": True}})
    assert ok.modo_respostas == "auto"
    with pytest.raises(ConfigError, match="cabem no máximo"):
        config_from_dict({**BASE, "agenda": {"posts_por_dia": 9, "janela_horas": [8, 16], "espacamento_min_horas": 2}})
    with pytest.raises(ConfigError, match="2 pilares"):
        config_from_dict({"perfil": {"nicho": "n"}, "pilares": [{"id": "a", "descricao": "A"}]})
    no_thread = config_from_dict({**BASE, "posts": {"permitir_threads": False}})
    assert "thread" not in no_thread.formatos
    only = config_from_dict({**BASE, "formatos": ["lista", "pergunta"]})
    assert list(only.formatos) == ["lista", "pergunta"]


# ---------------------------------------------------------------- otimizador
def _synthetic(n=300, seed=0):
    rng = np.random.default_rng(seed)
    dims = {"formato": ["lista", "pergunta", "opiniao"], "hora": [str(h) for h in range(8, 23)]}
    true = {("formato", "lista"): 1.0, ("hora", "20"): 1.0, ("hora", "12"): 0.6}
    obs = []
    for _ in range(n):
        c = {"formato": str(rng.choice(dims["formato"])), "hora": str(rng.choice(dims["hora"]))}
        y = 5 + true.get(("formato", c["formato"]), 0) + true.get(("hora", c["hora"]), 0) + rng.normal(0, 0.7)
        obs.append(Observation(c, {}, y, age_days=0))
    return dims, obs


def test_bandit_recovers_true_effects():
    dims, obs = _synthetic()
    m = LinearThompson(FeatureSpace(dims, []), seed=1).fit(obs)
    assert m.effects("formato")[0]["valor"] == "lista"
    assert m.effects("hora")[0]["valor"] == "20"
    hours = choose_hours(m, list(range(8, 23)), n=2, min_gap=2, explore=0.0, rng=random.Random(0))
    assert [h for h, _ in hours] == [12, 20]
    picks = [choose_content(m, {"formato": dims["formato"]}, 0.0, random.Random(i))[0]["formato"] for i in range(50)]
    assert picks.count("lista") >= 45


def test_choose_hours_respects_gap_and_window():
    m = LinearThompson(FeatureSpace({"hora": [str(h) for h in range(7, 23)]}, []), seed=3)
    for s in range(30):
        hours = [h for h, _ in choose_hours(m, list(range(7, 23)), 4, 3, 0.5, random.Random(s))]
        assert len(hours) == 4
        assert all(7 <= h < 23 for h in hours)
        assert all(b - a >= 3 for a, b in zip(hours, hours[1:]))


def test_discount_forgets_old_data():
    dims = {"formato": ["a", "b"]}
    old = [Observation({"formato": "a"}, {}, 10, age_days=120) for _ in range(30)] + \
          [Observation({"formato": "b"}, {}, 5, age_days=120) for _ in range(30)]
    new = [Observation({"formato": "a"}, {}, 5, age_days=1) for _ in range(8)] + \
          [Observation({"formato": "b"}, {}, 10, age_days=1) for _ in range(8)]
    m = LinearThompson(FeatureSpace(dims, []), discount=0.97, seed=0).fit(old + new)
    assert m.effects("formato")[0]["valor"] == "b"


# ---------------------------------------------------------------- prompts
def test_prompts_carry_profile_and_rules(cfg):
    sp = system_prompt_post(cfg)
    assert cfg.nicho in sp and "Sem links" in sp and "automatizada" in sp
    up = user_prompt_post(cfg, PostBrief("mitos", "thread", "humor", "numero", recentes=["x"], feedback=["muito longo"]))
    assert "REJEITADA" in up and "entre 3 e" in up
    assert post_tool(cfg)["input_schema"]["properties"]["posts"]["maxItems"] == cfg.max_tweets_thread


def test_adaptive_memory_shortens_only_when_audience_changes():
    from xgrowth.bandit import fit_adaptive
    rng = np.random.default_rng(0)
    space = FeatureSpace({"formato": ["a", "b"]}, [])
    def data(flip):
        X, y, ages = [], [], []
        for age in np.linspace(90, 0, 240):
            f = "a" if rng.random() < 0.5 else "b"
            good = ("b" if age < 20 else "a") if flip else "a"
            X.append(space.encode({"formato": f}, {}))
            y.append(5 + (1.5 if f == good else 0) + rng.normal(0, 0.5))
            ages.append(age)
        return np.array(X), np.array(y), np.array(ages)
    _, d_stable = fit_adaptive(space, *data(False))
    m, d_shift = fit_adaptive(space, *data(True))
    assert d_stable == 0.97 and d_shift < 0.97
    assert m.effects("formato")[0]["valor"] == "b"
