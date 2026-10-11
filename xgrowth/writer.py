"""Escrita de posts e respostas com o Claude (API da Anthropic), com saída estruturada via ferramenta."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


@dataclass
class PostBrief:
    pilar: str
    formato: str
    tom: str
    gancho: str
    recentes: list[str] = field(default_factory=list)
    melhores: list[str] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)


@dataclass
class PostDraft:
    tema: str
    tweets: list[str]


@dataclass
class ReplyBrief:
    autor: str
    mensagem: str
    post_original: str | None


@dataclass
class ReplyDecision:
    responder: bool
    texto: str
    motivo: str


# ---------------------------------------------------------------------------
# Prompts (funções puras, testáveis)
# ---------------------------------------------------------------------------
def system_prompt_post(cfg: Config) -> str:
    evitar = "\n".join(f"- {e}" for e in cfg.evitar) or "- (nada específico)"
    exemplos = "\n".join(f"- {e}" for e in cfg.exemplos) or "- (nenhum)"
    return f"""Você escreve os posts de um perfil no X (antigo Twitter).

PERFIL
- Nicho: {cfg.nicho}
- Público: {cfg.publico}
- Voz: {cfg.voz}
- Idioma: {cfg.idioma}

EXEMPLOS DA VOZ IDEAL (referência de estilo, não copie)
{exemplos}

REGRAS INEGOCIÁVEIS
- Cada post tem no máximo 270 caracteres.
- Sem links, sem @menções, no máximo 1 hashtag no total (de preferência nenhuma).
- Nada de pedir curtida, RT, follow ou "comenta aí" genérico. Uma pergunta genuína é permitida.
- Não invente dados, preços, benchmarks, estatísticas, citações ou notícias. Se não tiver certeza de um número, escreva sem número.
- O perfil é identificado como conta automatizada: não finja ser humano nem relate experiências pessoais que não aconteceram. Use cenários ("quem nunca...", "situação comum: ...").
- Nada de atacar pessoas, marcas ou grupos, nem polêmica gratuita: posts que geram bloqueios, "não tenho interesse" e denúncias derrubam o alcance da conta inteira no algoritmo do X.
- A primeira linha precisa prender a atenção sozinha (ela decide se a pessoa para de rolar o feed).
- Escreva como alguém do nicho escreveria no X: natural, específico, sem clichê de marketing, sem emojis em excesso.

EVITAR
{evitar}

Entregue o resultado chamando a ferramenta entregar_post."""


def user_prompt_post(cfg: Config, b: PostBrief) -> str:
    fmt_desc = cfg.formatos.get(b.formato, b.formato)
    n_hint = f" (entre 3 e {cfg.max_tweets_thread} posts)" if b.formato == "thread" else " (1 post só)"
    rec = "\n".join(f"- {t}" for t in b.recentes[:25]) or "- (nenhum ainda)"
    best = "\n".join(f"- {t}" for t in b.melhores[:5]) or "- (ainda sem dados)"
    fb = ""
    if b.feedback:
        fb = "\n\nA TENTATIVA ANTERIOR FOI REJEITADA PELOS MOTIVOS ABAIXO. Corrija:\n" + "\n".join(f"- {x}" for x in b.feedback)
    return f"""Escreva um novo post.

Pilar de conteúdo: {b.pilar} — {cfg.pilares.get(b.pilar, '')}
Formato: {b.formato} — {fmt_desc}{n_hint}
Tom: {b.tom} — {cfg.tons.get(b.tom, '')}
Gancho de abertura: {b.gancho} — {cfg.ganchos.get(b.gancho, '')}

Posts recentes do perfil (NÃO repita tema, estrutura nem frases):
{rec}

Posts do perfil que tiveram melhor desempenho (inspire-se no que funcionou, sem copiar):
{best}{fb}"""


def post_tool(cfg: Config) -> dict:
    return {
        "name": "entregar_post",
        "description": "Entrega o post pronto para publicação.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tema": {"type": "string", "description": "Tema do post em até 6 palavras."},
                "posts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": max(cfg.max_tweets_thread, 1),
                    "description": "Texto de cada post, na ordem. 1 item para post único; vários para thread.",
                },
            },
            "required": ["tema", "posts"],
        },
    }


def system_prompt_reply(cfg: Config) -> str:
    return f"""Você responde, em nome de um perfil do X sobre "{cfg.nicho}", a pessoas que interagiram com ele primeiro (responderam a um post ou mencionaram o perfil). O perfil é identificado como conta automatizada.

Voz do perfil: {cfg.voz}

REGRAS
- Uma resposta curta (até 240 caracteres), específica ao que a pessoa disse, útil ou simpática.
- Sem links, sem @menções, sem hashtags. Não peça follow, curtida nem RT.
- Não invente fatos, números ou promessas. Se não souber, diga que não sabe.
- Não finja ser humano. Se perguntarem se é bot, confirme que é uma conta automatizada.
- Responda no idioma da pessoa.
- NÃO responda (responder=false) quando: for spam, ofensa, provocação vazia, assunto sensível (saúde, política, tragédia, religião), pedido para parar, mensagem que não pede nem merece resposta (ex.: só um emoji), ou quando responder poderia constranger a pessoa.

Entregue a decisão chamando a ferramenta decidir_resposta."""


def user_prompt_reply(b: ReplyBrief) -> str:
    orig = b.post_original or "(a pessoa mencionou o perfil fora de um post nosso)"
    return f"""Post original do perfil:
\"\"\"{orig}\"\"\"

Mensagem de @{b.autor}:
\"\"\"{b.mensagem}\"\"\"
"""


REPLY_TOOL = {
    "name": "decidir_resposta",
    "description": "Decide se responde e com qual texto.",
    "input_schema": {
        "type": "object",
        "properties": {
            "responder": {"type": "boolean"},
            "texto": {"type": "string", "description": "Texto da resposta (vazio se responder=false)."},
            "motivo": {"type": "string", "description": "Motivo curto da decisão."},
        },
        "required": ["responder", "texto", "motivo"],
    },
}


# ---------------------------------------------------------------------------
# Implementação com o Claude
# ---------------------------------------------------------------------------
class ClaudeWriter:
    def __init__(self, cfg: Config, api_key: str):
        import anthropic

        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=api_key)

    def _tool(self, system: str, user: str, tool: dict, max_tokens: int = 1500) -> dict:
        resp = self.client.messages.create(
            model=self.cfg.modelo,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise RuntimeError("o modelo não retornou a ferramenta esperada")

    def write_post(self, brief: PostBrief) -> PostDraft:
        out = self._tool(system_prompt_post(self.cfg), user_prompt_post(self.cfg, brief), post_tool(self.cfg))
        tweets = [str(t).strip() for t in out.get("posts", []) if str(t).strip()]
        return PostDraft(tema=str(out.get("tema", "")).strip(), tweets=tweets)

    def write_reply(self, brief: ReplyBrief) -> ReplyDecision:
        out = self._tool(system_prompt_reply(self.cfg), user_prompt_reply(brief), REPLY_TOOL, max_tokens=600)
        return ReplyDecision(
            responder=bool(out.get("responder")),
            texto=str(out.get("texto", "")).strip(),
            motivo=str(out.get("motivo", "")).strip(),
        )
