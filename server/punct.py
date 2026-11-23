"""Grammar-based punctuation for streamed dictation.

Units are cut at pauses and Qwen decodes each one as a complete sentence, so a hesitation
("我希望由……Opus 5.5") comes out as "我希望由。Opus 5.5". A text-only punctuation model
(CT-Transformer zh-en via sherpa-onnx, CPU, ~5 ms per call) decides from the words on both sides
whether the grammar wants a break between two units, and which hesitation commas inside a unit
to drop. Filler words (嗯/呃) are removed. Only marks are changed, never words.
"""
from __future__ import annotations

import logging
import re

CJK_MARKS = "，。、；：！？…"
ASCII_MARKS = ",.?!;:"
MODEL_MARKS = "，。？、；：！"
FILLER = re.compile(r"(?:(?<=^)|(?<=[，。！？、；：,.!?;:\s]))(?:嗯+|呃+|额+|唔+|emm+|uhm*|um+)(?=[，、,。.\s]|$)[，、,。.\s]*",
                    re.I)
# "好，嗯，用 NPU": a one-character interjection right before a filler is part of the hesitation
LEAD_IN = re.compile(r"^(?:好|对|那|就|嗯)[，、,]\s*(?=(?:嗯+|呃+|额+)(?:[，、,。.\s]|$))")


def _is_mark(text: str, i: int) -> bool:
    """A movable mark: CJK marks, and ASCII ,.?!;: that are not inside a token (0.6B, v1.2, a,b)."""
    c = text[i]
    if c in CJK_MARKS:
        return True
    if c in ASCII_MARKS:
        prev = text[i - 1] if i > 0 else " "
        nxt = text[i + 1] if i + 1 < len(text) else " "
        return not (prev.isalnum() and nxt.isalnum())
    return False


def segment(text: str) -> tuple[str, list[str], list[str]]:
    """Split into (prefix, chars, seps): chars are the non-space, non-mark characters; seps[k] is
    the text between chars[k] and chars[k+1] (the last one: everything after the last char)."""
    chars, seps, prefix = [], [], ""
    for i, c in enumerate(text):
        if c.isspace() or _is_mark(text, i):
            if chars:
                seps[-1] += c
            else:
                prefix += c
        else:
            chars.append(c)
            seps.append("")
    return prefix, chars, seps


def marks_of(sep: str) -> str:
    return "".join(c for c in sep if not c.isspace())


def _latin(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _cjk(c: str) -> bool:
    return "\u3400" <= c <= "\u9fff"


def bare_sep(left: str, right: str, had_space: bool) -> str:
    """Separator once a mark is removed: Qwen's style keeps one space between Latin and anything else."""
    if (_latin(left) or _latin(right)) and (had_space or _latin(left) != _latin(right) or _latin(left)):
        return " "
    return ""


def drop_fillers(text: str) -> str:
    t = FILLER.sub("", LEAD_IN.sub("", text))
    return t if t.strip(" " + CJK_MARKS + ASCII_MARKS) else text


TRAIL_PUNCT = "。．.，,、！!？?；;：:…"
FILLER_ONLY = re.compile(r"[\s，。、,.!?！？…]*(?:(?:嗯+|呃+|额+|唔+|emm+|um+)[\s，。、,.!?！？…]*)+", re.I)
# "嗯，确实" after a finished sentence is an answer ("yes, indeed"), not a hesitation
AFFIRM = re.compile(r"^(嗯+)[，,]\s*(?=确实|对|是的|是啊|没错|好的|好|行|可以|明白|知道|当然|OK|ok|没问题)")
# a pause after these is a hesitation inside a phrase, never a clause boundary
OPEN_END = re.compile(r"(?:这个|那个|这些|那些|一个|一些|这|那|的|地|得|和|与|跟|或|及|把|被|给|对|向|从|在|用|让|将|"
                      r"就|是|要|会|能|想|说|也|都|还|再|又|很|更|最|不|没|没有|可以|应该|需要|可能|能够|已经|正在|"
                      r"而且|但是|因为|所以|如果|然后|还有|比如|以及|通过|关于|由|为|我|你|他|她|它|"
                      r"我们|你们|他们|the|a|an|of|to|and|or|with|in|on|for)$", re.I)
SENTENCE_END = "。．.？?！!"


def join_plain(parts: list[str]) -> str:
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and re.match(r"[A-Za-z0-9]", p[0]) and re.search(r"[A-Za-z0-9.,;:!?]$", out):
            out += " "
        out += p
    return out


def commit_unit(P, committed: str, held: str, text: str, gap: float | None, comma_gap: float,
                fillers: bool = True, grammar: bool = True, trace: list | None = None,
                floor_gap: float | None = None, stop_gap: float = 99.0) -> tuple[str, str]:
    """Append one decoded unit to the committed text. Returns (committed, held): `held` is the unit's own
    trailing mark, kept back until the next unit shows whether the sentence goes on."""
    if floor_gap is None:
        floor_gap = comma_gap
    text = text.strip()
    affirm = ""
    lead_filler = False
    if text and fillers:
        if committed and FILLER_ONLY.fullmatch(text):
            return committed, held  # a unit that is only a hesitation
        m = AFFIRM.match(text)
        if m and committed and held in SENTENCE_END:
            affirm = m.group(1)[0]
        stripped = drop_fillers(text).strip()
        lead_filler = bool(stripped) and not text.startswith(stripped[:2])
        text = stripped
    if not text:
        return committed, held
    tail = ""
    if text[-1] in TRAIL_PUNCT:
        text, tail = text[:-1], text[-1]
    r = None
    if committed and text and grammar and P is not None:
        try:
            r = P.fix(committed, text, held, gap, comma_gap)
        except Exception as e:
            logging.getLogger("asr").warning("punctuation failed: %s", e)
            r = None
    if r is not None:
        join, text = r
        model_join = join
        left = committed.rstrip()
        paused = gap is not None and (gap >= floor_gap or lead_filler)  # 嗯/呃 is part of the pause
        if not join.strip() and held in SENTENCE_END + "，,；;" and paused \
                and not OPEN_END.search(left) and (_cjk(left[-1:] or "a") or _cjk(text[:1] or "a")):
            # Qwen ended the unit and the speaker paused: the model alone is not enough to glue them
            join = "，"
        if model_join == "，" and held in "。．" and gap is not None and gap >= stop_gap:
            join = "。"  # a long pause after a finished sentence: keep Qwen's full stop
        if affirm:
            join = held if held in "？！?!" else "。"
            text = f"{affirm}，{text}"
        if trace is not None:
            trace.append({"left": committed[-12:], "held": held, "gap": gap, "model": model_join, "join": join,
                          "unit": text[:12]})
        committed = committed + join + text
    elif committed or held:
        punct = held
        starts_cjk = bool(re.match(r"[\u3400-\u9fff]", text))
        if punct in "。．" and gap is not None and gap < comma_gap and starts_cjk:
            punct = "，"
        if punct == "." and gap is not None and gap < comma_gap and not starts_cjk:
            punct = ","
        if not punct and starts_cjk and gap is not None and gap >= comma_gap:
            punct = "，"
        committed = join_plain([committed + punct, text])
    else:
        committed = text
    return committed, tail


class Punctuator:
    def __init__(self, model: str, threads: int = 1, context_chars: int = 60):
        import sherpa_onnx
        cfg = sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=model, num_threads=threads))
        self.p = sherpa_onnx.OfflinePunctuation(cfg)
        self.context_chars = context_chars

    def breaks(self, chars: list[str], spaced: list[bool]) -> list[str] | None:
        """The mark the model puts after each char ("" = none); None if the output cannot be aligned.
        `spaced[k]`: a word boundary follows chars[k] (keeps "VS Code" two words for the model)."""
        bare = "".join(c + (" " if spaced[k] else "") for k, c in enumerate(chars))
        out = self.p.add_punctuation(bare.strip())
        marks = [""] * len(chars)
        j = 0
        for c in out:
            if c.isspace():
                continue
            if j < len(chars) and c == chars[j]:
                j += 1
            elif c in MODEL_MARKS and j > 0:
                marks[j - 1] = c
        return marks if j == len(chars) else None

    def fix(self, left: str, unit: str, held: str, gap: float | None = None, comma_gap: float = 1.0,
            policy: str = "keep") -> tuple[str, str] | None:
        """`left`: text already committed (without the held mark); `unit`: the new unit without its
        trailing mark; `held`: Qwen's mark at the end of `left`; `gap`: pause before the unit (s).
        Returns (join separator, new unit), or None when the model output could not be used.
        The model decides whether the grammar wants a break at the join; its strength follows the
        model, softened to a comma after a short pause. Inside the unit Qwen's marks are kept
        (policy "keep"); "remove"/"replace" also let the model drop/replace them (tested: too eager)."""
        _lp, lchars, lseps = segment(left[-self.context_chars:]) if left else ("", [], [])
        prefix, uchars, useps = segment(unit)
        if not uchars:
            return None
        spaced = [" " in s and _latin(c) for c, s in zip(lchars, lseps)]
        if lchars:
            spaced[-1] = _latin(lchars[-1]) and _latin(uchars[0])
        spaced += [" " in s and _latin(c) for c, s in zip(uchars, useps)]
        marks = self.breaks(lchars + uchars, spaced)
        if marks is None:
            return None
        n = len(lchars)
        join = None
        if lchars:
            m = marks[n - 1]
            if held and held in "？！?!":
                join = held
            elif not m:
                join = bare_sep(lchars[-1], uchars[0], False)
            else:
                join = "？" if m == "？" else "！" if m == "！" else "。" if m == "。" else "，"
                if join == "。" and gap is not None and gap < comma_gap:
                    join = "，"
                if not _cjk(lchars[-1]) and not _cjk(uchars[0]):
                    join = {"，": ", ", "。": ". ", "？": "? ", "！": "! "}[join]
        out = [prefix.lstrip()]
        for k, c in enumerate(uchars):
            out.append(c)
            sep = useps[k]
            if k == len(uchars) - 1:
                out.append(sep)
                break
            old = marks_of(sep)
            new = marks[n + k]
            if not old or policy == "keep" or old[-1] in "？！?!":
                out.append(sep)
            elif not new:
                out.append(bare_sep(c, uchars[k + 1], " " in sep))
            elif policy == "replace" and old in "，。；、" and new in "，。；":
                out.append(sep.replace(old, new, 1))
            else:
                out.append(sep)
        return join, "".join(out)
