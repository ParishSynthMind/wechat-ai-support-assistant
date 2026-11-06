"""The knowledge base: Markdown files in knowledge_base/en and knowledge_base/zh, split into
sections at each "## " heading, searched with BM25. Chinese has no spaces, so Chinese text
is indexed as single characters plus pairs of characters (bigrams), which is the usual
simple approach and works well for short support questions."""
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_cjk_run = re.compile(f"[{CJK}]+")
_word = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
STOP = set("a an and are as at be by can do does for from how i if in is it me my of on or our so the this to "
           "we what when where which who why will with you your".split()) | set("的了吗呢是我你在有和吧啊么")


def tokens(text: str) -> List[str]:
    text = text.lower()
    out = [w for w in _word.findall(text) if w not in STOP]
    for run in _cjk_run.findall(text):
        out += [c for c in run if c not in STOP]
        out += [run[i:i + 2] for i in range(len(run) - 1)]
    return out


@dataclass
class Section:
    id: str        # e.g. en/shipping.md#Delivery times
    lang: str
    source: str
    heading: str
    text: str


@dataclass
class Hit:
    section: Section
    score: float


class KnowledgeBase:
    def __init__(self, sections: List[Section], k1: float = 1.4, b: float = 0.75):
        self.sections, self.k1, self.b = sections, k1, b
        self._tf = [Counter(tokens(s.heading + " " + s.heading + " " + s.text)) for s in sections]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Dict[str, int] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(sections)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def load(cls, folder) -> "KnowledgeBase":
        folder = Path(folder)
        sections: List[Section] = []
        for path in sorted(folder.glob("*/*.md")):
            lang = path.parent.name
            heading, buf = None, []
            for line in path.read_text(encoding="utf-8").splitlines() + ["## "]:
                if line.startswith("## "):
                    if heading and "".join(buf).strip():
                        rel = f"{lang}/{path.name}"
                        sections.append(Section(f"{rel}#{heading}", lang, rel, heading, "\n".join(buf).strip()))
                    heading, buf = line[3:].strip(), []
                elif heading is not None:
                    buf.append(line)
        return cls(sections)

    def search(self, query: str, lang: str = "", limit: int = 4, min_score: float = 0.5) -> List[Hit]:
        q = tokens(query)
        hits = []
        for i, s in enumerate(self.sections):
            tf = self._tf[i]
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    score += self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg))
            if score >= min_score:
                # Same-language sections first; the other language is still allowed (the model
                # answers in the follower's language either way).
                hits.append(Hit(s, score * (1.0 if not lang or s.lang == lang else 0.6)))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]
