"""Which language to answer in. Followers write in Chinese, English or a mix ("请问 shipping
to Dubai 多久?"). Chinese characters count for more than Latin letters, because one
character carries about as much as a short word; any real share of them means Chinese."""
import re

from .kb import CJK

_cjk = re.compile(f"[{CJK}]")
_latin = re.compile(r"[A-Za-z]")


def detect(text: str, default: str = "en") -> str:
    cjk = len(_cjk.findall(text or ""))
    latin = len(_latin.findall(text or ""))
    if cjk == 0 and latin == 0:
        return default
    return "zh" if cjk * 4 >= latin else "en"
