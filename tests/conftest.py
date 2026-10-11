import itertools
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xgrowth.config import load_config  # noqa: E402
from xgrowth.db import DB  # noqa: E402
from xgrowth.engine import Engine  # noqa: E402
from xgrowth.writer import PostDraft, ReplyDecision  # noqa: E402
from xgrowth.x_client import DryRunXClient  # noqa: E402

WORDS = ("placa cooler fonte gabinete monitor teclado mouse driver bios memoria ssd nvme rede latencia "
         "overclock undervolt temperatura ventoinha pasta termica cabo hdmi displayport resolucao escala "
         "shader cache vsync gsync freesync frametime gargalo processador nucleo thread clock energia").split()


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeWriter:
    def __init__(self, bad_first: int = 0):
        self.n = itertools.count()
        self.rng = random.Random(1)
        self.bad_first = bad_first
        self.post_calls = []
        self.reply_calls = []

    def _text(self, k=8):
        return " ".join(self.rng.sample(WORDS, k))

    def write_post(self, brief):
        self.post_calls.append(brief)
        i = next(self.n)
        if i < self.bad_first:
            return PostDraft(tema="ruim", tweets=["veja em https://exemplo.com @alguem"])
        if brief.formato == "thread":
            return PostDraft(tema=f"tema {i}", tweets=[f"{i}.{j} {self._text()}" for j in range(3)])
        return PostDraft(tema=f"tema {i}", tweets=[f"{i} {self._text()}"])

    def write_reply(self, brief):
        self.reply_calls.append(brief)
        if "spam" in brief.mensagem.lower():
            return ReplyDecision(False, "", "spam")
        return ReplyDecision(True, "Valeu! Boa observação.", "comentário relevante")


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.example.yaml")
    c.dry_run = True
    return c


@pytest.fixture
def clock():
    # 12/10/2026 00:05 em São Paulo (UTC-3)
    return Clock(datetime(2026, 10, 12, 3, 5, tzinfo=timezone.utc))


@pytest.fixture
def make_engine(cfg, clock):
    def _make(notifier=None, writer=None, seed=7, **overrides):
        for k, v in overrides.items():
            setattr(cfg, k, v)
        db = DB(":memory:")
        x = DryRunXClient(clock=clock, verbose=False)
        w = writer or FakeWriter()
        eng = Engine(cfg, db, x, w, notifier=notifier, clock=clock, seed=seed, report_path=None)
        return eng
    return _make
