"""Validações de texto: limite de caracteres do X, links, menções, repetição."""
from __future__ import annotations

import re
import unicodedata

MAX_WEIGHTED = 280

_URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|io|gg|tv|me|ly|co|app|dev|xyz|br)(?:\.br)?(?:/\S*)?\b)",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"(?<![\w@])@\w{1,15}")
_HASHTAG_RE = re.compile(r"(?<![\w#])#\w+")
_BAIT_RE = re.compile(
    r"\b(curte (e|esse|a[ií])|d[áaêe] (um )?like|deixa (seu|o|um) like|rt se|retu[ií]te (se|esse)|compartilhe (esse|este) post|"
    r"me siga|siga[- ]me|segue (a gente|o perfil|a conta)|follow me)\b",
    re.IGNORECASE,
)


def weighted_length(text: str) -> int:
    """Contagem aproximada do X (twitter-text v3): Latin/Cirílico etc. = 1, demais (CJK, emoji) = 2.

    É conservadora: sequências de emoji com ZWJ contam um pouco a mais do que o X conta.
    """
    text = unicodedata.normalize("NFC", text)
    total = 0
    for ch in text:
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            total += 1
        else:
            total += 2
    return total


def has_url(text: str) -> bool:
    return bool(_URL_RE.search(text))


def has_mention(text: str) -> bool:
    return bool(_MENTION_RE.search(text))


def hashtag_count(text: str) -> int:
    return len(_HASHTAG_RE.findall(text))


def is_engagement_bait(text: str) -> bool:
    return bool(_BAIT_RE.search(text))


_SCORE_RE = re.compile(r"(?<![\w-])(\d{1,2})\s*[xX×]\s*(\d{1,2})(?![\w-])")
_YEAR_RE = re.compile(r"(?<!\d)(19[4-9]\d|20[01]\d|202[0-4])(?!\d)")


def unsupported_facts(text: str, fatos: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Placares (2x1) e anos (1997) citados no texto que NÃO aparecem nos fatos informados.
    É o principal tipo de "fato" que modelos de linguagem inventam em conteúdo esportivo."""
    known = " ".join(fatos)
    known_scores = {(a, b) for a, b in _SCORE_RE.findall(known)}
    out = []
    for a, b in _SCORE_RE.findall(text):
        if (a, b) not in known_scores:
            out.append(f"placar {a}x{b}")
    known_years = set(_YEAR_RE.findall(known))
    for y in _YEAR_RE.findall(text):
        if y not in known_years:
            out.append(f"ano {y}")
    return list(dict.fromkeys(out))


def problems_for_post(tweets: list[str], max_tweets: int, max_len: int = MAX_WEIGHTED,
                      fatos: list[str] | tuple[str, ...] = ()) -> list[str]:
    probs: list[str] = []
    if not tweets:
        return ["nenhum post retornado"]
    if len(tweets) > max_tweets:
        probs.append(f"thread com {len(tweets)} posts (máximo {max_tweets})")
    total_tags = 0
    for i, t in enumerate(tweets, 1):
        t = t.strip()
        if not t:
            probs.append(f"post {i} vazio")
            continue
        n = weighted_length(t)
        if n > max_len:
            probs.append(f"post {i} tem {n} caracteres ponderados (máximo {max_len})")
        if has_url(t):
            probs.append(f"post {i} contém link (links custam 13x mais na API e derrubam o alcance)")
        if has_mention(t):
            probs.append(f"post {i} contém @menção (menções não solicitadas são proibidas para contas automatizadas)")
        if is_engagement_bait(t):
            probs.append(f"post {i} pede curtida/RT/follow (engagement bait)")
        for u in unsupported_facts(t, fatos):
            probs.append(f"post {i} cita {u}, que não está nos fatos atuais (não invente fatos; fale em termos gerais)")
        total_tags += hashtag_count(t)
    if total_tags > 1:
        probs.append(f"{total_tags} hashtags (máximo 1)")
    return probs


def problems_for_reply(text: str, max_len: int = 260) -> list[str]:
    probs = []
    t = (text or "").strip()
    if not t:
        return ["resposta vazia"]
    if weighted_length(t) > max_len:
        probs.append("resposta longa demais")
    if has_url(t):
        probs.append("resposta com link")
    if has_mention(t):
        probs.append("resposta com @menção")
    if is_engagement_bait(t):
        probs.append("resposta pede curtida/follow")
    return probs


def _norm_words(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]{3,}", t)
    return set(words)


def similarity(a: str, b: str) -> float:
    """Jaccard entre conjuntos de palavras (>= 3 letras)."""
    wa, wb = _norm_words(a), _norm_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def max_similarity(text: str, others: list[str]) -> float:
    return max((similarity(text, o) for o in others), default=0.0)


def matches_opt_out(text: str, phrases: list[str]) -> bool:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _MENTION_RE.sub(" ", t)
    for p in phrases:
        p2 = unicodedata.normalize("NFKD", p.lower())
        p2 = "".join(c for c in p2 if not unicodedata.combining(c))
        if re.search(r"(?<!\w)" + re.escape(p2) + r"(?!\w)", t):
            return True
    return False
