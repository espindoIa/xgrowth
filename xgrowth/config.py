"""Carregamento e validação da configuração (config.yaml) e dos segredos (.env / variáveis de ambiente)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Catálogo padrão de "braços" que o otimizador testa. Pode ser restringido ou
# ampliado no config.yaml (chaves formatos / tons / ganchos).
# ---------------------------------------------------------------------------
FORMATOS_PADRAO = {
    "opiniao_curta": "Uma opinião forte e bem fundamentada em 1 a 2 frases curtas.",
    "pergunta": "Uma pergunta genuína que convida o público a responder com a própria experiência ou opinião.",
    "lista": "Lista curta (3 a 5 itens) de dicas práticas, um item por linha.",
    "thread": "Thread: o 1º post é um gancho forte que funciona sozinho; os seguintes entregam valor concreto; o último fecha com uma conclusão.",
    "cenario": "Uma situação comum do nicho (descrita como cenário, nunca como experiência pessoal inventada) com uma lição no final.",
    "comparacao": "Comparação direta entre duas opções (A vs B), com veredito claro e o motivo.",
    "mito_vs_fato": "Desmente um mito comum do nicho e explica o que é verdade.",
}
TONS_PADRAO = {
    "direto": "Direto ao ponto, frases curtas, zero enrolação.",
    "humor": "Bem-humorado, com uma sacada leve; o humor nunca às custas de alguém.",
    "didatico": "Didático, explica o porquê de forma simples.",
    "provocativo": "Provocativo mas respeitoso: desafia uma crença comum sem atacar pessoas ou grupos.",
}
GANCHOS_PADRAO = {
    "numero": "Abre com um número concreto (que seja verdadeiro e verificável).",
    "contraintuitivo": "Abre com uma afirmação que contraria o senso comum.",
    "pergunta": "Abre com uma pergunta.",
    "afirmacao_forte": "Abre com uma afirmação curta e categórica.",
}
PESOS_ACOES_PADRAO = {
    # Aproximação dos sinais que o ranqueador do X valoriza (reply, repost, quote,
    # share, clique no perfil -> seguir). Curtida é o sinal mais barato.
    "likes": 1.0,
    "bookmarks": 2.0,
    "replies": 3.0,
    "reposts": 4.0,
    "quotes": 4.0,
    "profile_clicks": 5.0,
}


@dataclass
class Config:
    # perfil
    nicho: str
    publico: str
    voz: str
    idioma: str = "pt-BR"
    evitar: list[str] = field(default_factory=list)
    exemplos: list[str] = field(default_factory=list)
    pilares: dict[str, str] = field(default_factory=dict)
    formatos: dict[str, str] = field(default_factory=lambda: dict(FORMATOS_PADRAO))
    tons: dict[str, str] = field(default_factory=lambda: dict(TONS_PADRAO))
    ganchos: dict[str, str] = field(default_factory=lambda: dict(GANCHOS_PADRAO))
    # agenda
    fuso: str = "America/Sao_Paulo"
    posts_por_dia: int = 4
    hora_inicio: int = 7
    hora_fim: int = 23
    espacamento_min_horas: int = 2
    tolerancia_atraso_min: int = 90
    intervalo_ciclo_min: int = 30     # de quanto em quanto tempo o agendador roda o tick
    # otimizador
    horizonte_horas: float = 24.0
    exploracao: float = 0.03          # calibrado na simulação (ver README)
    desconto_diario: float | str = "auto"  # "auto" = memória de ~23 dias que encurta se o público mudar
    escala_amostragem: float = 0.35   # < 1 = Thompson mais "decidido"; calibrado na simulação
    peso_views: float = 1.0
    peso_engajamento: float = 1.0
    pesos_acoes: dict[str, float] = field(default_factory=lambda: dict(PESOS_ACOES_PADRAO))
    # posts
    modo_posts: str = "auto"  # auto | aprovar
    antecedencia_rascunho_horas: float = 1.0
    antecedencia_aprovacao_horas: float = 12.0
    max_tweets_thread: int = 5
    # respostas
    modo_respostas: str = "aprovar"  # off | aprovar | auto
    aprovacao_x_respostas_ia: bool = False
    max_respostas_dia: int = 30
    max_respostas_usuario_dia: int = 2
    opt_out: list[str] = field(default_factory=list)
    # llm
    modelo: str = "claude-sonnet-5-5"
    # segurança
    dry_run: bool = True
    pausado: bool = False
    max_posts_dia_absoluto: int = 12
    db_path: str = "data/estado.db"

    # --- derivados -------------------------------------------------------
    @property
    def horas(self) -> list[int]:
        return list(range(self.hora_inicio, self.hora_fim))

    def dimensoes(self) -> dict[str, list[str]]:
        """Dimensões que o otimizador testa (cada valor é um 'braço')."""
        return {
            "pilar": list(self.pilares),
            "formato": list(self.formatos),
            "tom": list(self.tons),
            "gancho": list(self.ganchos),
            "hora": [str(h) for h in self.horas],
        }


def _pick(d: dict, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _as_named(items, default: dict[str, str]) -> dict[str, str]:
    """Aceita lista de ids (filtra o catálogo padrão), lista de {id, descricao} ou dict."""
    if items is None:
        return dict(default)
    out: dict[str, str] = {}
    if isinstance(items, dict):
        return {str(k): str(v) for k, v in items.items()}
    for it in items:
        if isinstance(it, str):
            if it not in default:
                raise ConfigError(f"'{it}' não existe no catálogo padrão; use {{id, descricao}} para criar um novo.")
            out[it] = default[it]
        elif isinstance(it, dict) and "id" in it:
            out[str(it["id"])] = str(it.get("descricao") or default.get(it["id"], it["id"]))
        else:
            raise ConfigError(f"Item inválido: {it!r}")
    return out


def config_from_dict(raw: dict) -> Config:
    perfil = raw.get("perfil") or {}
    if not perfil.get("nicho"):
        raise ConfigError("Defina perfil.nicho no config.yaml")
    pilares = _as_named(raw.get("pilares"), {})
    if not pilares:
        raise ConfigError("Defina ao menos 2 pilares de conteúdo em 'pilares'.")

    cfg = Config(
        nicho=str(perfil["nicho"]),
        publico=str(perfil.get("publico", "")),
        voz=str(perfil.get("voz", "")),
        idioma=str(perfil.get("idioma", "pt-BR")),
        evitar=list(perfil.get("evitar") or []),
        exemplos=list(perfil.get("exemplos_de_posts") or []),
        pilares=pilares,
        formatos=_as_named(raw.get("formatos"), FORMATOS_PADRAO),
        tons=_as_named(raw.get("tons"), TONS_PADRAO),
        ganchos=_as_named(raw.get("ganchos"), GANCHOS_PADRAO),
    )
    ag = raw.get("agenda") or {}
    cfg.fuso = ag.get("fuso", cfg.fuso)
    cfg.posts_por_dia = int(ag.get("posts_por_dia", cfg.posts_por_dia))
    janela = ag.get("janela_horas", [cfg.hora_inicio, cfg.hora_fim])
    cfg.hora_inicio, cfg.hora_fim = int(janela[0]), int(janela[1])
    cfg.espacamento_min_horas = int(ag.get("espacamento_min_horas", cfg.espacamento_min_horas))
    cfg.tolerancia_atraso_min = int(ag.get("tolerancia_atraso_min", cfg.tolerancia_atraso_min))
    cfg.intervalo_ciclo_min = int(ag.get("intervalo_ciclo_min", cfg.intervalo_ciclo_min))

    ot = raw.get("otimizador") or {}
    cfg.horizonte_horas = float(ot.get("horizonte_recompensa_horas", cfg.horizonte_horas))
    cfg.exploracao = float(ot.get("exploracao_forcada", cfg.exploracao))
    d = ot.get("desconto_diario", cfg.desconto_diario)
    cfg.desconto_diario = "auto" if str(d).strip().lower() == "auto" else float(d)
    cfg.escala_amostragem = float(ot.get("escala_amostragem", cfg.escala_amostragem))
    cfg.peso_views = float(ot.get("peso_views", cfg.peso_views))
    cfg.peso_engajamento = float(ot.get("peso_engajamento", cfg.peso_engajamento))
    if ot.get("pesos_acoes"):
        cfg.pesos_acoes.update({k: float(v) for k, v in ot["pesos_acoes"].items()})

    po = raw.get("posts") or {}
    cfg.modo_posts = po.get("modo", cfg.modo_posts)
    cfg.antecedencia_rascunho_horas = float(po.get("antecedencia_rascunho_horas", cfg.antecedencia_rascunho_horas))
    cfg.antecedencia_aprovacao_horas = float(po.get("antecedencia_aprovacao_horas", cfg.antecedencia_aprovacao_horas))
    cfg.max_tweets_thread = int(po.get("max_posts_por_thread", cfg.max_tweets_thread))
    if not po.get("permitir_threads", True):
        cfg.formatos.pop("thread", None)

    re_ = raw.get("respostas") or {}
    cfg.modo_respostas = re_.get("modo", cfg.modo_respostas)
    cfg.aprovacao_x_respostas_ia = bool(re_.get("tenho_aprovacao_do_x_para_respostas_ia", False))
    cfg.max_respostas_dia = int(re_.get("max_por_dia", cfg.max_respostas_dia))
    cfg.max_respostas_usuario_dia = int(re_.get("max_por_usuario_por_dia", cfg.max_respostas_usuario_dia))
    cfg.opt_out = [s.lower() for s in (re_.get("frases_opt_out") or [
        "pare", "stop", "para de me responder", "pare de me responder",
        "não me responda", "nao me responda", "me deixa em paz", "unsubscribe",
    ])]

    cfg.modelo = _pick(raw, "llm", "modelo", default=cfg.modelo)

    se = raw.get("seguranca") or {}
    cfg.dry_run = bool(se.get("dry_run", cfg.dry_run))
    cfg.pausado = bool(se.get("pausado", cfg.pausado))
    cfg.max_posts_dia_absoluto = int(se.get("max_posts_por_dia_absoluto", cfg.max_posts_dia_absoluto))
    cfg.db_path = se.get("arquivo_estado", cfg.db_path)

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if len(cfg.pilares) < 2:
        raise ConfigError("Use pelo menos 2 pilares para o otimizador ter o que comparar.")
    if not (0 <= cfg.hora_inicio < cfg.hora_fim <= 24):
        raise ConfigError("agenda.janela_horas deve ser [inicio, fim] com 0 <= inicio < fim <= 24.")
    if not 1 <= cfg.intervalo_ciclo_min <= 50:
        raise ConfigError("agenda.intervalo_ciclo_min deve estar entre 1 e 50.")
    if cfg.posts_por_dia < 1:
        raise ConfigError("agenda.posts_por_dia deve ser >= 1.")
    if cfg.posts_por_dia > cfg.max_posts_dia_absoluto:
        raise ConfigError("posts_por_dia maior que seguranca.max_posts_por_dia_absoluto.")
    if cfg.modo_posts not in ("auto", "aprovar"):
        raise ConfigError("posts.modo deve ser 'auto' ou 'aprovar'.")
    if cfg.modo_respostas not in ("off", "aprovar", "auto"):
        raise ConfigError("respostas.modo deve ser 'off', 'aprovar' ou 'auto'.")
    if cfg.modo_respostas == "auto" and not cfg.aprovacao_x_respostas_ia:
        raise ConfigError(
            "respostas.modo = 'auto' exige aprovação prévia do X para respostas geradas por IA "
            "(formulário: https://help.x.com/forms/platform). Depois de aprovado, marque "
            "respostas.tenho_aprovacao_do_x_para_respostas_ia: true. Até lá use 'aprovar'."
        )
    if not 0 <= cfg.exploracao <= 0.5:
        raise ConfigError("otimizador.exploracao_forcada deve estar entre 0 e 0.5.")
    if not 0.05 <= cfg.escala_amostragem <= 2.0:
        raise ConfigError("otimizador.escala_amostragem deve estar entre 0.05 e 2.")
    if cfg.desconto_diario != "auto" and not 0.8 <= cfg.desconto_diario <= 1.0:
        raise ConfigError("otimizador.desconto_diario deve ser 'auto' ou um número entre 0.8 e 1.0.")
    span = cfg.hora_fim - cfg.hora_inicio
    max_posts_janela = 1 + (span - 1) // max(cfg.espacamento_min_horas, 1)
    if cfg.posts_por_dia > max_posts_janela:
        raise ConfigError(
            f"Com janela de {span}h e espaçamento de {cfg.espacamento_min_horas}h cabem no máximo "
            f"{max_posts_janela} posts por dia."
        )


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Arquivo de configuração não encontrado: {p}. Copie config.example.yaml para config.yaml.")
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return config_from_dict(raw)


# ---------------------------------------------------------------------------
# Segredos
# ---------------------------------------------------------------------------
@dataclass
class Secrets:
    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_secret: str | None = None
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @property
    def has_x(self) -> bool:
        return all([self.x_api_key, self.x_api_secret, self.x_access_token, self.x_access_secret])

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Leitor mínimo de .env (não sobrescreve variáveis já definidas)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def secrets_from_env() -> Secrets:
    g = lambda k: (os.environ.get(k) or "").strip() or None  # noqa: E731
    return Secrets(
        x_api_key=g("X_API_KEY"),
        x_api_secret=g("X_API_SECRET"),
        x_access_token=g("X_ACCESS_TOKEN"),
        x_access_secret=g("X_ACCESS_SECRET"),
        anthropic_api_key=g("ANTHROPIC_API_KEY"),
        telegram_bot_token=g("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=g("TELEGRAM_CHAT_ID"),
    )
