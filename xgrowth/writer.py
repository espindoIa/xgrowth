"""Escrita de posts e respostas com o Gemini (API do Google), com saída estruturada em JSON."""
from __future__ import annotations

import json
import re
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
    dia_de_jogo: bool = False
    fatos: list[str] | None = None   # None = usa os fatos fixos do config


@dataclass
class PostDraft:
    tema: str
    tweets: list[str]


@dataclass
class ReplyBrief:
    autor: str
    mensagem: str
    post_original: str | None
    fatos: list[str] | None = None


@dataclass
class ReplyDecision:
    responder: bool
    texto: str
    motivo: str


# ---------------------------------------------------------------------------
# Prompts (funções puras, testáveis)
# ---------------------------------------------------------------------------
def _fatos_block(cfg: Config, fatos: list[str] | None = None) -> str:
    fatos = cfg.fatos if fatos is None else fatos
    if fatos:
        linhas = "\n".join(f"- {f}" for f in fatos)
        return (f"FATOS ATUAIS (única fonte de informações concretas que você pode afirmar)\n{linhas}")
    return ("FATOS ATUAIS\n- (nenhum informado: não afirme nada concreto sobre resultados, placares, datas, "
            "escalações, contratações, estatísticas ou títulos; não critique nem elogie o desempenho do time, da "
            "comissão técnica ou da diretoria atuais; fale de emoção, opinião e cenários)")


def system_prompt_post(cfg: Config, fatos: list[str] | None = None) -> str:
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

{_fatos_block(cfg, fatos)}

REGRAS INEGOCIÁVEIS
- Cada post tem no máximo 270 caracteres.
- Sem links, sem @menções, no máximo 1 hashtag no total (de preferência nenhuma).
- Nada de pedir curtida, RT, follow ou "comenta aí" genérico. Uma pergunta genuína é permitida.
- Não invente dados, preços, estatísticas, placares, datas, citações ou notícias. Só afirme um fato concreto se ele estiver em FATOS ATUAIS acima. Na dúvida, escreva sem o número ou o nome.
- O perfil é identificado como conta automatizada: não finja ser humano nem relate experiências pessoais que não aconteceram. Use cenários ("quem nunca...", "situação comum: ...").
- Opinião forte e provocação são bem-vindas quando a voz do perfil pede, mas sempre com argumento ou sacada. Provoque ideias, atitudes, situações e rivalidades entre clubes; nunca pessoas pelo que elas são (origem, cor, gênero, orientação, aparência, religião, deficiência), nem com ofensa pessoal ou incentivo à violência. Posts que geram bloqueios, "não tenho interesse" e denúncias derrubam o alcance da conta inteira no algoritmo do X.
- Crítica a pessoas reais (jogadores, técnico, dirigentes) é permitida quando há fato em FATOS ATUAIS que a sustente: critique a decisão, a atuação ou a gestão (o que foi feito), nunca a pessoa. Não acuse ninguém de crime, corrupção ou má-fé, não fale da vida pessoal e não invente o que alguém disse ou fez.
- A primeira linha precisa prender a atenção sozinha (ela decide se a pessoa para de rolar o feed).
- Escreva como alguém do nicho escreveria no X: natural, específico, sem clichê de marketing, sem emojis em excesso.

EVITAR
{evitar}

Responda somente com o JSON no formato pedido."""


def user_prompt_post(cfg: Config, b: PostBrief) -> str:
    fmt_desc = cfg.formatos.get(b.formato, b.formato)
    n_hint = f" (entre 3 e {cfg.max_tweets_thread} posts)" if b.formato == "thread" else " (1 post só)"
    rec = "\n".join(f"- {t}" for t in b.recentes[:25]) or "- (nenhum ainda)"
    best = "\n".join(f"- {t}" for t in b.melhores[:5]) or "- (ainda sem dados)"
    jogo = "\nHoje é dia de jogo: o clima é de expectativa, mas não invente adversário, horário nem resultado." if b.dia_de_jogo else ""
    fb = ""
    if b.feedback:
        fb = "\n\nA TENTATIVA ANTERIOR FOI REJEITADA PELOS MOTIVOS ABAIXO. Corrija:\n" + "\n".join(f"- {x}" for x in b.feedback)
    return f"""Escreva um novo post.

Pilar de conteúdo: {b.pilar} — {cfg.pilares.get(b.pilar, '')}
Formato: {b.formato} — {fmt_desc}{n_hint}
Tom: {b.tom} — {cfg.tons.get(b.tom, '')}
Gancho de abertura: {b.gancho} — {cfg.ganchos.get(b.gancho, '')}{jogo}

Posts recentes do perfil (NÃO repita tema, estrutura nem frases):
{rec}

Posts do perfil que tiveram melhor desempenho (inspire-se no que funcionou, sem copiar):
{best}{fb}"""


def post_schema(cfg: Config) -> dict:
    """JSON Schema da resposta de um post (o Gemini é obrigado a segui-lo)."""
    return {
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
    }


def system_prompt_reply(cfg: Config, fatos: list[str] | None = None) -> str:
    return f"""Você responde, em nome de um perfil do X sobre "{cfg.nicho}", a pessoas que interagiram com ele primeiro (responderam a um post ou mencionaram o perfil). O perfil é identificado como conta automatizada.

Voz do perfil: {cfg.voz}

{_fatos_block(cfg, fatos)}

REGRAS
- Uma resposta curta (até 240 caracteres), específica ao que a pessoa disse, útil ou simpática.
- Sem links, sem @menções, sem hashtags. Não peça follow, curtida nem RT.
- Não invente fatos, números, placares ou promessas: só afirme o que estiver em FATOS ATUAIS. Se não souber, diga que não sabe.
- Não finja ser humano. Se perguntarem se é bot, confirme que é uma conta automatizada.
- Responda no idioma da pessoa.
- Mesmo que a voz do perfil seja provocativa, nunca humilhe nem ataque quem escreveu: discorde da ideia com argumento ou brinque de leve. Na dúvida, seja simpático.
- NÃO responda (responder=false) quando: for spam, ofensa, provocação vazia, assunto sensível (saúde, política, tragédia, religião), pedido para parar, mensagem que não pede nem merece resposta (ex.: só um emoji), ou quando responder poderia constranger a pessoa.

Responda somente com o JSON no formato pedido."""


def user_prompt_reply(b: ReplyBrief) -> str:
    orig = b.post_original or "(a pessoa mencionou o perfil fora de um post nosso)"
    return f"""Post original do perfil:
\"\"\"{orig}\"\"\"

Mensagem de @{b.autor}:
\"\"\"{b.mensagem}\"\"\"
"""


REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "responder": {"type": "boolean"},
        "texto": {"type": "string", "description": "Texto da resposta (vazio se responder=false)."},
        "motivo": {"type": "string", "description": "Motivo curto da decisão."},
    },
    "required": ["responder", "texto", "motivo"],
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json_reply(text: str) -> dict:
    """Lê o JSON devolvido pelo modelo (tolera cerca de markdown, que o modo JSON não deveria gerar)."""
    data = json.loads(_FENCE.sub("", text or "").strip())
    if not isinstance(data, dict):
        raise ValueError("o modelo não retornou um objeto JSON")
    return data


# ---------------------------------------------------------------------------
# Implementação com o Gemini
# ---------------------------------------------------------------------------
class GeminiWriter:
    def __init__(self, cfg: Config, api_key: str, client=None):
        self.cfg = cfg
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self.client = client

    def _json(self, system: str, user: str, schema: dict, max_tokens: int = 2048) -> dict:
        from google.genai import types

        kw = dict(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            # o limite inclui os tokens de raciocínio: não baixe muito, ou a resposta vem cortada
            max_output_tokens=max_tokens,
        )
        if self.cfg.raciocinio:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=self.cfg.raciocinio)
        resp = self.client.models.generate_content(
            model=self.cfg.modelo, contents=user, config=types.GenerateContentConfig(**kw))
        text = getattr(resp, "text", None)
        if not text:
            reason = None
            cands = getattr(resp, "candidates", None) or []
            if cands:
                reason = getattr(cands[0], "finish_reason", None)
            raise RuntimeError(f"o Gemini não retornou texto (motivo: {reason or 'desconhecido'}; "
                               "pode ter sido bloqueio de segurança ou limite de tokens)")
        return parse_json_reply(text)

    def ping(self) -> str:
        """Chamada mínima para o `verificar` conferir a chave e o modelo."""
        out = self._json("Responda com o JSON pedido.", 'Devolva {"ok": true}.',
                         {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                         max_tokens=256)
        return "ok" if out.get("ok") else "resposta inesperada"

    def write_post(self, brief: PostBrief) -> PostDraft:
        out = self._json(system_prompt_post(self.cfg, brief.fatos), user_prompt_post(self.cfg, brief), post_schema(self.cfg))
        tweets = [str(t).strip() for t in out.get("posts", []) if str(t).strip()]
        return PostDraft(tema=str(out.get("tema", "")).strip(), tweets=tweets)

    def write_reply(self, brief: ReplyBrief) -> ReplyDecision:
        out = self._json(system_prompt_reply(self.cfg, brief.fatos), user_prompt_reply(brief), REPLY_SCHEMA, max_tokens=1024)
        return ReplyDecision(
            responder=bool(out.get("responder")),
            texto=str(out.get("texto", "")).strip(),
            motivo=str(out.get("motivo", "")).strip(),
        )
