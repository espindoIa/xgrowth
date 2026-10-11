import random

import numpy as np
import pytest

from xgrowth.bandit import FeatureSpace, LinearThompson, Observation, choose_content, choose_hours
from xgrowth.config import ConfigError, config_from_dict
from xgrowth.textutil import (matches_opt_out, problems_for_post, problems_for_reply, similarity,
                              unsupported_facts, weighted_length)
from xgrowth.writer import post_schema, system_prompt_post, user_prompt_post, PostBrief


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
    assert post_schema(cfg)["properties"]["posts"]["maxItems"] == cfg.max_tweets_thread


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


# ---------------------------------------------------------------- futebol: fatos, config e dia de jogo
def test_unsupported_facts_blocks_invented_scores_and_old_years():
    assert unsupported_facts("Foi 3x1 em 1997") == ["placar 3x1", "ano 1997"]
    assert unsupported_facts("Foi 3x1 em 1997", ["Cruzeiro 3 x 1 em 1997"]) == [] or \
        unsupported_facts("Foi 3x1 em 1997", ["Cruzeiro 3x1 em 1997"]) == []
    assert unsupported_facts("esquema 4-3-3, R$ 1.500 em 2026, jogo às 16h") == []
    assert any("placar" in p for p in problems_for_post(["Ganhamos de 2 x 0 ontem"], 1))
    assert problems_for_post(["Ganhamos de 2x0 ontem"], 1, fatos=["O Cruzeiro venceu por 2x0."]) == []


def test_cruzeiro_config_is_valid_and_safe_by_default():
    from pathlib import Path
    from xgrowth.config import load_config
    c = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert "Cruzeiro" in c.nicho and c.dry_run is True and c.modo_respostas == "aprovar"
    assert c.modelo.startswith("gemini") and len(c.pilares) >= 4
    assert "numero" not in c.ganchos  # abrir com número convida o modelo a inventar estatística
    assert all(problems_for_post([e], 1) == [] for e in c.exemplos)
    sp = system_prompt_post(c)
    assert "nenhum informado" in sp and "automatizada" in sp


def test_context_facts_and_game_days_in_config_and_prompts():
    cfg = config_from_dict({**BASE, "contexto": {"fatos_atuais": ["Domingo tem jogo no Mineirão."],
                                                 "dias_de_jogo": ["2026-10-18"]}})
    assert cfg.dias_de_jogo == {"2026-10-18"}
    assert "Domingo tem jogo no Mineirão." in system_prompt_post(cfg)
    up = user_prompt_post(cfg, PostBrief("a", "lista", "humor", "cena", dia_de_jogo=True))
    assert "dia de jogo" in up
    with pytest.raises(ConfigError, match="AAAA-MM-DD"):
        config_from_dict({**BASE, "contexto": {"dias_de_jogo": ["18/10/2026"]}})


def test_game_day_control_is_learned_separately_from_hour():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from xgrowth.learning import controls_for
    tz = ZoneInfo("America/Sao_Paulo")
    at = datetime(2026, 10, 18, 19, 0, tzinfo=timezone.utc)  # 16h em São Paulo
    assert controls_for(at, 100, tz, {"2026-10-18"})["dia_de_jogo"] == 1.0
    assert controls_for(at, 100, tz)["dia_de_jogo"] == 0.0
    late = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)  # 22h do dia 18 em SP: ainda é dia de jogo
    assert controls_for(late, 100, tz, {"2026-10-18"})["dia_de_jogo"] == 1.0


def test_requer_fatos_config_and_prompt_rules():
    cfg = config_from_dict({"perfil": {"nicho": "n"}, "pilares": [
        {"id": "a", "descricao": "A"}, {"id": "b", "descricao": "B"},
        {"id": "critica", "descricao": "C", "requer_fatos": True}]})
    assert cfg.pilares_requer_fatos == {"critica"}
    with pytest.raises(ConfigError, match="sem 'requer_fatos'"):
        config_from_dict({"perfil": {"nicho": "n"}, "pilares": [
            {"id": "a", "descricao": "A"}, {"id": "b", "descricao": "B", "requer_fatos": True},
            {"id": "c", "descricao": "C", "requer_fatos": True}]})
    sp = system_prompt_post(cfg, ["Perdemos em casa."])
    assert "Perdemos em casa." in sp and "nunca a pessoa" in sp and "crime, corrupção" in sp
    assert "não critique nem elogie" in system_prompt_post(cfg, [])
    from xgrowth.config import load_config
    from pathlib import Path
    real = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert real.pilares_requer_fatos == {"cobranca_ao_clube"}
