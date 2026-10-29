"""
German Context Builder
Turns a YouTube link into a German study handout (PDF) you can take handwritten notes on.

Pipeline:
    1. Download the transcript (German preferred)
    2. Spoken -> standard German: minimal edits to fix grammar, punctuation and
       capitalization, with every change recorded
    3. Extract key grammar (e.g. how to use um ... zu), key vocabulary
       (English + Chinese), and idiomatic expressions usable in daily life,
       at work and in interviews
    4. Generate the PDF: the corrected transcript first (wide line spacing and
       side margins for notes), followed by the extracted highlights

Dependencies:
    pip install youtube-transcript-api google-genai reportlab
"""

# ── Gemini API key ────────────────────────────────────────────────────────────
# No need to edit this. The key is stored in config.json in the same folder
# (shared with german_vocab.py).
GEMINI_API_KEY = ""
# ─────────────────────────────────────────────────────────────────────────────

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import subprocess
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

# 模型降级链：前面的质量更好，503（过载）时自动往后退。
# flash-lite 放最后 —— 它很快但偷懒，有时会原样返回不做修正，属于"总比没有强"。
MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash-lite",
]
RETRY_ROUNDS = 3           # 整条链轮几遍

# 免费额度是「每个模型每天 N 次请求」，所以省请求比省 token 重要得多。
# 模型上下文窗口有 100 万 token，一小时的字幕也就几千 token —— 正常视频
# 一次就全送进去，不分块。只有超长的才切，避免单次输出被截断。
CHUNK_SIZE = 25_000        # 字符。超过才分块（约等于 7000 token）
MAX_TRANSCRIPT = 60_000    # 超长视频的保护上限

# GUI 会把自己的日志函数挂上来，这样重试过程在界面里看得见
log_hook = lambda msg: None

# 本次运行中已确认「今日额度用完」的模型，不再浪费时间去试
_EXHAUSTED: set[str] = set()


class EmptyResponse(RuntimeError):
    """模型返回了空结果（被安全过滤挡下，或思考把 token 吃光）—— 换个模型重试即可。"""


def _error_kind(err: Exception) -> str:
    """把异常分类，决定是等待、换模型、还是直接放弃。

    day      —— 日配额耗尽。等待毫无意义（要等到第二天重置），必须立刻换模型。
    minute   —— 每分钟限流。等几秒就能恢复。
    overload —— 服务端过载（503/500）。换个模型往往立刻就通。
    other    —— 401/404/参数错误之类，重试也没用，直接抛。
    """
    if isinstance(err, EmptyResponse):
        return "overload"
    m = str(err)
    if "RESOURCE_EXHAUSTED" in m or "429" in m:
        low = m.lower()
        return "day" if ("perday" in low.replace("_", "") or "per day" in low) else "minute"
    if any(k in m for k in ("503", "500", "UNAVAILABLE", "INTERNAL",
                            "overloaded", "high demand", "DEADLINE_EXCEEDED")):
        return "overload"
    return "other"


def _suggested_delay(err: Exception, default: float) -> float:
    """Google 在 429 里会直接告诉你该等多久，有就用它的。"""
    m = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", str(err))
    if not m:
        m = re.search(r"retry in (\d+(?:\.\d+)?)s", str(err))
    return min(float(m.group(1)) + 1, 60) if m else default


def check_and_import():
    missing = []
    try:
        import google.genai as _
    except ImportError:
        missing.append("google-genai")
    try:
        import youtube_transcript_api as _
    except ImportError:
        missing.append("youtube-transcript-api")
    try:
        import reportlab as _
    except ImportError:
        missing.append("reportlab")
    return missing


# ─────────────────────────────────────────────
# Step 1 — Transcript
# ─────────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    patterns = [r"(?:v=|youtu\.be/|embed/|shorts/|live/)([A-Za-z0-9_-]{11})"]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


_CAPTION_NOISE = re.compile(r"\[(?:Musik|Music|Applaus|Applause|Gelächter|Laughter|.*?)\]")


def _join_entries(entries) -> str:
    text = " ".join(e.text.replace("\n", " ") for e in entries)
    text = _CAPTION_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_transcript(video_id: str) -> tuple[str, str]:
    """Returns (transcript_text, language_code). 优先德语。"""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()

    for langs in (["de"], ["de-DE"], ["de-AT"], ["de-CH"]):
        try:
            return _join_entries(api.fetch(video_id, languages=langs)), langs[0]
        except Exception:
            continue

    # 没有直接命中就列出全部字幕轨，先挑德语，再退而求其次
    try:
        available = list(api.list(video_id))
    except Exception as exc:
        raise RuntimeError(
            "拿不到字幕。请确认视频是公开的、并且有字幕（自动生成的也行）。"
        ) from exc

    german = [t for t in available if t.language_code.lower().startswith("de")]
    for t in german or available:
        try:
            return _join_entries(t.fetch()), t.language_code
        except Exception:
            continue

    raise RuntimeError("这个视频没有可下载的字幕轨。")


def chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """按词边界切块，保证不会把单词劈开。"""
    words, chunks, cur, n = text.split(), [], [], 0
    for w in words:
        if n + len(w) + 1 > size and cur:
            chunks.append(" ".join(cur))
            cur, n = [], 0
        cur.append(w)
        n += len(w) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks


# ─────────────────────────────────────────────
# Step 2 — 口语 → 标准德语（最小改动）
# ─────────────────────────────────────────────

CORRECT_PROMPT = """You are preparing raw YouTube auto-captions for a German learner to read.
The learner wants AUTHENTIC SPOKEN GERMAN, lightly cleaned up — NOT written German.

### WHAT TO DO
1. FORMATTING (always needed — auto-captions have none of this):
   add punctuation, sentence boundaries, and capitalization (Satzanfang, alle Nomen).
2. DELETE fillers (äh, ähm, hm), stutters, pure repetitions and caption artifacts.
3. REPAIR TRANSCRIPTION GARBAGE — this is a HARD RULE, not a judgement call.
   YouTube's auto-captioner constantly mishears words and writes strings that are not
   German words at all. The learner will try to look these up and be misled. Every token
   in your output must be a REAL word (German, or a normal loanword/brand as actually
   spelled). Go through the chunk and ask of each unusual token: does this word exist?
   If not, restore what the speaker obviously said, using the topic as your guide:
     "Innoki Pfanne"     -> "Gnocchi-Pfanne"
     "Preorkout Snack"   -> "Pre-Workout-Snack"
     "Leibfischkäse"     -> "Kräuterfrischkäse"
     "nichtich"          -> "nicht wirklich"
     "mit einfreme"      -> "mit einfrieren"
     "Süßkartoffelbows"  -> "Süßkartoffel-Bowls"
   Anglicisms, food words, brand names and compound nouns are mangled most often — check
   them hardest. Also fix run-together or wrongly split compounds (Reis kocher ->
   Reiskocher). If you genuinely cannot tell what was said, keep the token but make sure
   the sentence around it still parses — never invent new content to paper over a gap.

4. FIX grammar errors that would actively mislead a learner:
   - the conjugated verb not in final position in a dass- / ob- / wenn- / damit-clause
   - clearly wrong verb forms or wrong auxiliaries (hat gegangen -> ist gegangen)
   - case errors that change the meaning
   - sentences the speaker abandoned mid-way -> complete them in the smallest possible way

### THE MOST IMPORTANT RULE — LEAVE EVERYTHING ELSE ALONE
Natural spoken German is NOT an error. Do not "improve" it, do not make it more formal,
do not restructure it. Specifically, KEEP AS IS:
   - Modalpartikeln: ja, doch, mal, halt, eben, wohl, schon
   - "weil" and "obwohl" with main-clause word order — standard in speech, KEEP IT
   - short elliptical sentences, "und dann ..." chains, casual repetition for emphasis
   - colloquial vocabulary and contractions (gibt's, 'ne, mal eben, kriegen)
   - Vorfeld-Ellipse ("Hab ich schon gemacht.") and left dislocation
     ("Die Deutschen, die wollen ...")
When in doubt, DO NOT CHANGE IT. If a sentence sounds natural spoken out loud, it is fine.
Changing something that was already acceptable is a mistake, not an improvement.

BUT THIS RULE DOES NOT APPLY TO RULE 3. A string that is not a real word is never a
stylistic choice — it is always a transcription error, and it must always be repaired.
Restraint applies to grammar and register, never to garbled words.

### SEGMENTATION — THIS IS A HARD REQUIREMENT
Split the text into MANY SHORT SEGMENTS of 1-3 sentences each, in the original order.
A segment with more than 3 sentences is an ERROR. Never return the whole text as one
segment. A 10-minute video normally yields 50-100 segments. The learner reads these
numbered segments side by side with the translation, so they must stay short.

### TRANSLATION
Translate every segment into natural, fluent CHINESE (中文) — what a Chinese speaker would
actually say, not word-for-word. The learner reads this to understand context, including
specialist words they have no intention of memorising. So translate those properly too.

### OUTPUT
Return one JSON object, nothing else:
{
  "segments": [
    {"de": "Heute ist Sonntag, das heißt Meal-Prep-Tag.", "zh": "今天是周日，也就是备餐日。"},
    {"de": "Das lass ich auch nicht gelten.", "zh": "这个理由我也不认。"}
  ]
}
Every segment is 1-3 sentences and has its own "zh". Together the segments must cover the
ENTIRE input from the first word to the last. Never summarize, shorten or skip anything."""


ANALYZE_PROMPT = """You are an experienced German teacher (DaF) building a study sheet.
The learner's goal: LIVE AND WORK in German — everyday life, the office, job interviews.
Target level: {level}.

You receive a German transcript. Build the sheet from it.

### THE LEARNER'S EXPLICIT PREFERENCES — THESE OVERRIDE EVERYTHING ELSE
- NO isolated single words. EVERY entry must be a USAGE: a collocation, a fixed phrase,
  a verb + preposition pattern, or a whole reusable sentence.
    GOOD: "sich gesund ernähren" · "etwas nicht gelten lassen" · "Das kommt ganz darauf an."
    BAD:  "die Ernährung" · "gelten" · "der Anästhesist" · "die Narkose"
- IGNORE specialist jargon. If this is a doctor's vlog, skip the medical vocabulary
  completely; if it is about cars, skip the car parts. The learner will never say these
  words at work or in daily life, and the Chinese translation already handles comprehension.
  Ask yourself: would an ordinary person say this in an ordinary week? If not, DROP IT.
- LIMIT THE QUANTITY. Fewer, better entries. NEVER pad a list to reach a number —
  returning 10 excellent entries is much better than 16 padded ones.

### WHAT TO PRODUCE

1. "grammar_points" — 6 to 8 structures that actually appear in the transcript and deserve
   a mini-lesson (e.g. "um ... zu + Infinitiv", "Konjunktiv II für höfliche Bitten",
   "lassen + Infinitiv", "Relativsätze im Dativ").
   - "name": the structure, German
   - "level": A2 / B1 / B2 / C1
   - "structure": the formula, e.g. "um + (Objekt/Angaben) + zu + Infinitiv"
   - "usage_zh": 中文讲解 —— 什么时候用、为什么这么用，2-4 句话，要具体
   - "examples": 2-3 examples, each {{"de": "...", "en": "...", "zh": "..."}}.
     The FIRST should come from the transcript. At least one must be reusable at work
     or in an interview.
   - "pitfalls_zh": 中文 —— 中国学习者最常犯的错，给出 ✗ 错误示例 和 ✓ 正确示例
   - "compare_zh": 中文 —— 和易混结构对比（um zu vs. damit，wenn vs. als）。没有就给 ""

2. "phrases" — 10 to 16 USAGES, every one of them ACTUALLY SAID IN THE TRANSCRIPT.
   This is the heart of the sheet, and the rule below is absolute.

   ### THE HEADWORD MUST MATCH THE VIDEO — NO SUBSTITUTIONS
   Build "de" from the words the speaker really used. Do NOT replace the phrase with a
   more familiar synonym, and do NOT let the headword contradict the quote.
     Speaker says "Ich muss mir keine Gedanken darum machen, was ich esse."
       CORRECT  "de": "sich (Dat.) Gedanken machen um + Akk."
       WRONG    "de": "sich (Akk.) Sorgen machen um"   <- different phrase, different case
   Getting the reflexive case (Dat. vs Akk.) and the preposition right matters more than
   anything else here. If you are not certain the speaker used a phrase, leave it out.

   - "de": citable form of the phrase AS USED, with case and preposition marked
   - "en": English meaning of the phrase itself (a few words, not a sentence)
   - "zh": 这个用法本身的中文意思，几个字的释义 —— 例如 "健康饮食"、"不认可（某个理由）"。
     绝对不要把例句的整句翻译放进这里，那是 quote_zh 的位置。
   - "quote_de": the sentence from the transcript where it appears. Copy it, do not rewrite
     it. It must contain the phrase.
   - "quote_zh": 该句的中文翻译
   - "usage_zh": 中文 —— 怎么用、什么场合用、跟什么格或介词搭配，一两句话
   - "alternatives": 1 to 3 other ways to express the same idea — this is where a related
     phrase like "sich Sorgen machen um" belongs, NOT in the headword. Each:
     {{"de": "sich (Akk.) Sorgen machen um + Akk.", "zh": "担心某事",
       "note_zh": "语气更重，指真正的忧虑；Gedanken machen 更中性，只是「操心、费心思」"}}
     Give [] if nothing natural comes to mind. Never pad.
   - "level": A2 / B1 / B2 / C1 / häufig

3. "expressions" — 8 to 14 whole SENTENCES the speaker actually says that the learner can
   reuse in their own life. Same absolute rule: every one must come from the transcript.
   Pick the lines that are reusable OUTSIDE this video's topic — ways of organising a
   thought, softening, agreeing, explaining a reason, describing a routine or a plan.
   Skip anything that only makes sense for this specific video.
   - "de": the line from the transcript (you may trim it to the reusable core)
   - "en": English
   - "zh": 中文
   - "category": Alltag / Arbeit / Vorstellungsgespräch / Small Talk / Behörde / Meinung
     —— where the LEARNER could reuse it, not where it appeared
   - "register": formell / neutral / umgangssprachlich
   - "adapted_de": the same pattern rebuilt for the learner's own life, ideally work or
     interview related — this is what makes the line worth learning
   - "adapted_zh": 该改写句的中文翻译
   - "reuse_zh": 中文 —— 这个说法的骨架是什么、换什么场合能用，一句话

4. "title": a short German title for the video (max 6 words)
   "summary_de": 2-3 sentences in German
   "summary_zh": 2-3 句中文概括

### RULES
- Everything marked _zh MUST be Chinese. "en" MUST be English. German stays German.
- Never invent a transcript quote. If a sentence is not in the transcript, write your own
  natural example instead of pretending it came from the video.
- Return one JSON object only, no markdown fences, no commentary."""


def _safe_json_parse(raw: str) -> dict:
    """解析模型返回的 JSON；容忍 Python 字面量和被截断的输出。"""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    raw = re.sub(r"\bNone\b", "null", raw)
    raw = re.sub(r"\bTrue\b", "true", raw)
    raw = re.sub(r"\bFalse\b", "false", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # 被截断时，退回到最后一个完整的对象/字符串再补上收尾括号
        cuts = [m.end() for m in re.finditer(r"[}\"]", raw)][-60:]
        for cut in reversed(cuts):
            for suffix in ("]}", "}]}", '"]}', "}}]}"):
                try:
                    return json.loads(raw[:cut] + suffix)
                except json.JSONDecodeError:
                    continue
        lo, hi = max(0, exc.pos - 120), min(len(raw), exc.pos + 120)
        raise ValueError(
            f"JSON parse error — {exc.msg} at char {exc.pos}\n"
            f"Context: ...{raw[lo:hi]}..."
        ) from exc


def _gemini(api_key: str, system: str, user: str, max_tokens: int,
            temperature: float, thinking: int | None = 0, label: str = "") -> str:
    """调一次 Gemini。

    过载（503）时的策略：先横着换模型，再竖着等时间。
    第一轮把 MODELS 里的模型快速试一遍（只等几秒），实在都不行才拉长等待时间再来一轮。
    比死守一个模型等 40 秒有效得多 —— 高峰期往往只是某一个模型的某个容量池满了。

    thinking=0 关闭思考（快、省，也常常更容易排到队）；给正整数则限定思考预算。
    """
    import time
    import random
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    cfg_kwargs = dict(
        system_instruction=system,
        max_output_tokens=max_tokens,
        temperature=temperature,
        response_mime_type="application/json",
    )
    if thinking is not None:
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking)
        except Exception:
            pass  # 老版本 SDK 不支持就算了

    plan = [(m, rnd) for rnd in range(RETRY_ROUNDS) for m in MODELS]
    last_err: Exception | None = None
    tag = f"{label} " if label else ""

    for i, (model, rnd) in enumerate(plan):
        if model in _EXHAUSTED:          # 今天这个模型已经没额度了，别再试
            continue
        try:
            resp = client.models.generate_content(
                model=model,
                config=types.GenerateContentConfig(**cfg_kwargs),
                contents=user,
            )
            text = (resp.text or "").strip()
            if not text:
                raise EmptyResponse(f"{model} 返回了空结果")
            if model != MODELS[0]:
                log_hook(f"⚠  {tag}改用备用模型 {model} 完成")
            return text

        except Exception as e:
            last_err = e
            kind = _error_kind(e)

            if kind == "other":          # 参数错、key 错，重试没意义
                raise

            if kind == "day":
                # 日配额要等到第二天才重置，等待纯属浪费时间 —— 立刻换下一个模型。
                # 额度是按模型分别算的，所以换一个通常就能继续。
                _EXHAUSTED.add(model)
                remaining = [m for m in MODELS if m not in _EXHAUSTED]
                log_hook(f"⚠  {tag}{model} 今日免费额度已用完，"
                         + (f"立刻改用 {remaining[0]}" if remaining else "没有备用模型了"))
                continue                 # 注意：不 sleep

            # 找到下一个真正会执行的尝试（跳过已耗尽的模型）
            nxt = next(((plan[j][0], plan[j][1]) for j in range(i + 1, len(plan))
                        if plan[j][0] not in _EXHAUSTED), None)
            if nxt is None:
                break
            next_model, next_rnd = nxt

            if next_rnd == rnd and next_model != model:
                # 同一轮里换模型：不同模型是不同的容量池，这个满了不代表那个满，
                # 等待没有任何意义 —— 直接换。整条链试完一遍才值得停下来等。
                log_hook(f"↻  {tag}{model} 过载，立刻试 {next_model}")
                continue

            # 轮次边界：一整圈都不通，说明是大面积拥堵，这时才真的等
            if kind == "minute":         # 每分钟限流，Google 会告诉你等多久
                wait = _suggested_delay(e, 20)
                log_hook(f"⏳ {tag}每分钟限流，等 {wait:.0f}s 后重来一轮")
            else:
                wait = min(10 * (3 ** rnd) + random.uniform(0, 3), 60)
                log_hook(f"⏳ {tag}一轮 {len(MODELS)} 个模型都过载，"
                         f"等 {wait:.0f}s 后重来  (第 {rnd + 2}/{RETRY_ROUNDS} 轮)")
            time.sleep(wait)

    if _EXHAUSTED and not [m for m in MODELS if m not in _EXHAUSTED]:
        raise RuntimeError(
            f"{tag}今天所有模型的免费额度都用完了（每个模型每天 20 次请求）。"
            f"免费额度每天重置，明天再跑即可；已经处理完的部分会自动跳过。"
            f"想不受限制可以在 Google AI Studio 给这个 key 开通结算。"
        ) from last_err

    raise RuntimeError(
        f"{tag}所有模型都暂时不可用（已试 {len(plan)} 次）。"
        f"Gemini 正在高峰期，过几分钟再点一次即可 —— 已经处理完的部分会自动跳过，不用重头来。"
    ) from last_err


# 德语句末（排除常见缩写，避免在 z.B. / bzw. 处误切）
_DE_SENT = re.compile(
    r'(?<!\bz\.B)(?<!\bbzw)(?<!\busw)(?<!\bca)(?<!\bDr)(?<!\bNr)'
    r'(?<=[.!?])\s+(?=[A-ZÄÖÜ„"])')
_ZH_SENT = re.compile(r'(?<=[。！？])\s*')

MAX_SENTENCES = 3


def _resegment(segments: list[dict]) -> list[dict]:
    """兜底：模型有时无视分段要求，把整篇塞进一个 segment。

    这里按句子重新切成 1-3 句一段，并把对应的中文按比例分配过去 ——
    编号要能和 Teil 2 的翻译对上，所以段不能太长。
    """
    out: list[dict] = []
    for seg in segments:
        de_sents = [x.strip() for x in _DE_SENT.split(seg.get("de", "")) if x.strip()]
        if len(de_sents) <= MAX_SENTENCES:
            out.append(seg)
            continue

        zh_sents = [x.strip() for x in _ZH_SENT.split(seg.get("zh", "")) if x.strip()]
        groups = [de_sents[i:i + MAX_SENTENCES]
                  for i in range(0, len(de_sents), MAX_SENTENCES)]
        for g, de_part in enumerate(groups):
            # 中德句数通常不完全一致，按比例分配比硬配对稳妥
            lo = round(g * len(zh_sents) / len(groups))
            hi = round((g + 1) * len(zh_sents) / len(groups))
            out.append({"de": " ".join(de_part), "zh": "".join(zh_sents[lo:hi])})
    return out


def _fingerprint(transcript: str) -> str:
    """字幕内容、分块大小或输出结构变了，缓存就作废（v2 = 德中对照结构）。"""
    import hashlib
    return hashlib.sha1(f"{CHUNK_SIZE}:v2:{transcript}".encode()).hexdigest()[:16]


def correct_transcript(transcript: str, api_key: str, progress=lambda *_: None,
                       cache_file: Path | None = None) -> dict:
    """轻度整理字幕并翻译，返回 {"segments": [{"de": ..., "zh": ...}, ...]}。

    只修明显的语法错误，口语特征原样保留 —— 详见 CORRECT_PROMPT。
    每块跑完就落盘，中途失败后重跑会跳过已完成的块。
    """
    chunks = chunk_text(transcript)
    fp = _fingerprint(transcript)
    done: dict[int, dict] = {}

    if cache_file and cache_file.exists():
        try:
            blob = json.loads(cache_file.read_text())
            if blob.get("fingerprint") == fp:
                done = {int(k): v for k, v in blob.get("chunks", {}).items()}
                if done:
                    log_hook(f"↩  发现上次的进度，跳过已完成的 {len(done)}/{len(chunks)} 块")
        except Exception:
            done = {}

    for i, chunk in enumerate(chunks, 1):
        if i in done:
            progress(i, len(chunks))
            continue

        # 把上一块结尾的几句话带上，保证跨块衔接不断句
        prev = done.get(i - 1, {}).get("segments") or []
        tail = " ".join(prev[-1].get("de", "").split()[-25:]) if prev else ""
        context = (f"CONTEXT — the text just before this chunk "
                   f"(do NOT output it again):\n{tail}\n\n") if tail else ""
        user = f"{context}CHUNK ({i}/{len(chunks)}):\n{chunk}"

        progress(i, len(chunks))
        data = _safe_json_parse(_gemini(api_key, CORRECT_PROMPT, user,
                                        max_tokens=40000, temperature=0.15,
                                        thinking=0, label=f"[{i}/{len(chunks)}]"))
        segs = []
        for x in data.get("segments", []):
            # 容错：模型偶尔会退回成纯字符串数组
            de = (x.get("de") if isinstance(x, dict) else str(x)) or ""
            zh = (x.get("zh") if isinstance(x, dict) else "") or ""
            if de.strip():
                segs.append({"de": de.strip(), "zh": zh.strip()})
        done[i] = {"segments": segs}

        if cache_file:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(
                    {"fingerprint": fp, "chunks": done}, ensure_ascii=False))
            except Exception:
                pass   # 缓存写不进去不算错，继续跑

    segments: list[dict] = []
    for i in range(1, len(chunks) + 1):
        segments.extend(done.get(i, {}).get("segments", []))

    raw_count = len(segments)
    segments = _resegment(segments)
    if len(segments) > raw_count:
        log_hook(f"⚠  模型只给了 {raw_count} 段，已按句子重新切成 {len(segments)} 段")

    # 整篇一次送进去时，模型偶尔会"偷懒"只处理前半段就收尾。
    # 删掉填充词后本来就会略短，但少太多说明漏了内容。
    src_words = len(transcript.split())
    out_words = sum(len(x["de"].split()) for x in segments)
    if src_words and out_words / src_words < 0.6:
        log_hook(f"⚠  整理稿只有原文的 {out_words / src_words:.0%}，可能漏掉了后半段。"
                 f"把 CHUNK_SIZE 调小（比如 12000）再跑一次会更稳。")

    missing_zh = sum(1 for x in segments if not x["zh"])
    if missing_zh:
        log_hook(f"⚠  有 {missing_zh}/{len(segments)} 段没给出中文翻译")

    return {"segments": segments}


def _norm_words(text: str) -> list[str]:
    """归一化成词表：小写、去标点，便于做原文比对。"""
    return re.sub(r"[^\w\u00c0-\u024f ]", " ", str(text or "").lower()).split()


def _quoted_from(quote: str, haystack: str, n: int = 5) -> bool:
    """这句话真的出自字幕吗？

    比对连续 n 个词的片段，允许模型在标点和大小写上有出入，
    但不允许它把话改写或者干脆编一句。
    """
    toks = _norm_words(quote)
    if not toks:
        return False
    if len(toks) < n:
        return " ".join(toks) in haystack
    return any(" ".join(toks[i:i + n]) in haystack
               for i in range(len(toks) - n + 1))


def _drop_fabricated(data: dict, transcript: str) -> dict:
    """丢掉查无此句的条目 —— 整份讲义的价值就在于出自原文。"""
    haystack = " ".join(_norm_words(transcript))
    for key, field in (("phrases", "quote_de"), ("expressions", "de")):
        items = data.get(key) or []
        kept = [x for x in items if _quoted_from(x.get(field, ""), haystack)]
        if len(kept) < len(items):
            log_hook(f"⚠  {key}: 丢掉 {len(items) - len(kept)} 条字幕里查不到的（保留 {len(kept)} 条）")
        if items and not kept:
            log_hook(f"⚠  {key} 全部条目都对不上原文，这一节会是空的 —— 建议重跑一次")
        data[key] = kept
    return data


# 数量硬上限：prompt 里已经要求"少而精"，但弱模型（降级到 flash-lite 时）
# 会无视数量约束。在代码里再兜一层，保证讲义不会膨胀成几十页。
CAPS = {"grammar_points": 8, "phrases": 16, "expressions": 16}


def analyze_transcript(corrected: str, api_key: str, level: str) -> dict:
    """整理语法 / 用法 / 表达。所有引用的句子都会回字幕里核对一遍。"""
    # 给一个明确的思考预算，而不是用默认值：既保证整理质量，
    # 又比"不限量思考"更容易在高峰期排到队。
    data = _safe_json_parse(_gemini(
        api_key,
        ANALYZE_PROMPT.format(level=level),
        f"GERMAN TRANSCRIPT:\n\n{corrected}",
        max_tokens=32000,
        temperature=0.4,
        thinking=4096,
        label="[整理]",
    ))
    data = _drop_fabricated(data, corrected)
    for key, cap in CAPS.items():
        items = data.get(key) or []
        if len(items) > cap:
            log_hook(f"⚠  模型给了 {len(items)} 条 {key}，按上限截到 {cap} 条")
            items = items[:cap]
        data[key] = items
    return data


# ─────────────────────────────────────────────
# Step 4 — PDF
# ─────────────────────────────────────────────

# 同一套配色，和 german_vocab.py 保持一致
P = {
    "accent":   "#5C63B8",
    "accent_d": "#3D4391",
    "lav":      "#C5C8F0",
    "lav_pale": "#EEF2FF",
    "mint":     "#3F8F6B",
    "peach":    "#B56B3A",
    "text":     "#1A1D2E",
    "mid":      "#4B5163",
    "light":    "#8A90A6",
    "rule":     "#DFE2F2",
    "row":      "#F7F8FC",
}

# 抄写区：正文栏两侧各留约 36mm 做笔记
TR_MARGIN = 42      # mm，左右页边距（transcript 页）
NT_MARGIN = 20      # mm，左右页边距（笔记页）

_FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
    ("/Library/Fonts/Arial Unicode.ttf", None),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
]


def _register_font() -> str:
    """找一个同时能显示德语变元音和中文的字体。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path, idx in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            font = TTFont("Deutsch", path) if idx is None else TTFont("Deutsch", path, subfontIndex=idx)
            pdfmetrics.registerFont(font)
            pdfmetrics.registerFontFamily("Deutsch", normal="Deutsch", bold="Deutsch",
                                          italic="Deutsch", boldItalic="Deutsch")
            return "Deutsch"
        except Exception:
            continue

    # 最后的退路：内置中文 CID 字体
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:
        return "Helvetica"


def _styles(font: str) -> dict:
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor

    def st(name, size, leading, color=P["text"], **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading,
                              textColor=HexColor(color), **kw)

    return {
        "title":      st("title", 21, 27, P["accent_d"], spaceAfter=4),
        "subtitle":   st("subtitle", 12, 17, P["mid"], spaceAfter=10),
        "meta":       st("meta", 8.5, 13, P["light"]),
        "bar":        st("bar", 13, 18, "#FFFFFF"),
        "h2":         st("h2", 12, 17, P["accent_d"], spaceBefore=10, spaceAfter=4),
        # 正文行距 ≈ 2.3 倍，留出手写空间
        "transcript": st("transcript", 11.5, 27, P["text"], spaceAfter=13),
        "body":       st("body", 9.8, 14.5, P["text"]),
        # Teil 2 中文翻译：段落多，行距适中，段间留白便于对照编号
        "trans":      st("trans", 9.8, 16, P["mid"], spaceAfter=7),
        "body_zh":    st("body_zh", 9.8, 15.5, P["mid"]),
        "de":         st("de", 10, 14.5, P["accent_d"]),
        "en":         st("en", 9, 13, P["light"]),
        "zh":         st("zh", 9.5, 14, P["mid"]),
        "cell":       st("cell", 8.8, 12.5, P["text"]),
        "cell_de":    st("cell_de", 9, 12.8, P["accent_d"]),
        "cell_zh":    st("cell_zh", 8.8, 12.8, P["mid"]),
        "th":         st("th", 8.6, 12, "#FFFFFF"),
        "note":       st("note", 8.6, 12.5, P["light"], spaceBefore=2),
    }


def _p(text) -> str:
    return escape(str(text or "").strip())


def build_pdf(corrected: dict, analysis: dict, meta: dict, filepath: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
                                    Spacer, Table, TableStyle, NextPageTemplate,
                                    PageBreak, KeepTogether)

    font = _register_font()
    S = _styles(font)
    PW, PH = A4

    tr_x, tr_w = TR_MARGIN * mm, PW - 2 * TR_MARGIN * mm
    nt_x, nt_w = NT_MARGIN * mm, PW - 2 * NT_MARGIN * mm
    top, bottom = 20 * mm, 20 * mm

    def footer(canvas, doc, label):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(HexColor(P["light"]))
        canvas.drawString(nt_x, 11 * mm, label)
        canvas.drawRightString(PW - nt_x, 11 * mm, str(canvas.getPageNumber()))
        canvas.restoreState()

    def on_notes(canvas, doc):
        footer(canvas, doc, meta.get("footer", ""))

    def on_transcript(canvas, doc):
        # 两条竖虚线标出正文栏，线外就是记笔记的地方
        canvas.saveState()
        canvas.setStrokeColor(HexColor(P["rule"]))
        canvas.setLineWidth(0.7)
        canvas.setDash(1.5, 3)
        for x in (tr_x - 7 * mm, tr_x + tr_w + 7 * mm):
            canvas.line(x, bottom - 4 * mm, x, PH - top + 6 * mm)
        canvas.restoreState()
        footer(canvas, doc, meta.get("footer", ""))

    doc = BaseDocTemplate(
        filepath, pagesize=A4,
        title=analysis.get("title", "Deutsch"), author="German Context Builder",
        leftMargin=nt_x, rightMargin=nt_x, topMargin=top, bottomMargin=bottom,
    )
    doc.addPageTemplates([
        PageTemplate(id="notes",
                     frames=[Frame(nt_x, bottom, nt_w, PH - top - bottom, id="n")],
                     onPage=on_notes),
        PageTemplate(id="transcript",
                     frames=[Frame(tr_x, bottom, tr_w, PH - top - bottom, id="t")],
                     onPage=on_transcript),
    ])

    def bar(title_de, title_zh, width=nt_w):
        """整段色块标题"""
        p = Paragraph(f'{_p(title_de)}   <font size="9" color="{P["lav"]}">{_p(title_zh)}</font>', S["bar"])
        t = Table([[p]], colWidths=[width])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), HexColor(P["accent"])),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return [Spacer(1, 6), t, Spacer(1, 10)]

    def table(header, rows, widths):
        data = [[Paragraph(_p(h), S["th"]) for h in header]] + rows
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), HexColor(P["accent"])),
            ("GRID", (0, 0), (-1, -1), 0.4, HexColor(P["rule"])),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for i in range(1, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), HexColor(P["row"])))
        t.setStyle(TableStyle(style))
        return t

    story = []
    segments = corrected["segments"]

    # ── 封面 ──────────────────────────────────
    story.append(Paragraph(_p(analysis.get("title") or "Deutsch lernen mit Video"), S["title"]))
    story.append(Paragraph(
        f'Niveau {_p(meta.get("level"))} · {len(segments)} Segmente · '
        f'{len(analysis["phrases"])} Wendungen · {len(analysis["expressions"])} Redemittel',
        S["subtitle"]))

    if analysis.get("summary_de"):
        story.append(Paragraph(_p(analysis["summary_de"]), S["de"]))
        story.append(Spacer(1, 4))
    if analysis.get("summary_zh"):
        story.append(Paragraph(_p(analysis["summary_zh"]), S["zh"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f'{_p(meta.get("url"))}<br/>Untertitel: {_p(meta.get("lang"))} · '
        f'erstellt am {datetime.now().strftime("%Y-%m-%d %H:%M")}', S["meta"]))

    # ── Teil 1：字幕（两侧留白做笔记）────────────
    story.append(NextPageTemplate("transcript"))
    story.append(PageBreak())
    story.extend(bar("Teil 1 — Transkript", "字幕 · 两侧留白可做笔记", tr_w))
    for i, seg in enumerate(segments, 1):
        story.append(Paragraph(
            f'<font size="8" color="{P["lav"]}">{i:02d}</font>&nbsp;&nbsp;{_p(seg["de"])}',
            S["transcript"]))

    story.append(NextPageTemplate("notes"))
    story.append(PageBreak())

    # ── Teil 2：中文翻译（编号和上面一一对应）────
    if any(seg.get("zh") for seg in segments):
        story.extend(bar("Teil 2 — Übersetzung", "中文翻译 · 编号对应上面的字幕"))
        for i, seg in enumerate(segments, 1):
            if not seg.get("zh"):
                continue
            story.append(Paragraph(
                f'<font size="8" color="{P["lav"]}">{i:02d}</font>&nbsp;&nbsp;{_p(seg["zh"])}',
                S["trans"]))

    # ── Teil 3：重点语法 ────────────────────────
    if analysis["grammar_points"]:
        story.extend(bar("Teil 3 — Grammatik", "重点语法精讲"))
        for g in analysis["grammar_points"]:
            block = [Paragraph(
                f'<font color="{P["accent"]}">▍</font> {_p(g.get("name"))}'
                f'&nbsp;&nbsp;<font size="8" color="{P["light"]}">{_p(g.get("level"))}</font>',
                S["h2"])]
            if g.get("structure"):
                block.append(Paragraph(
                    f'<font color="{P["light"]}">Struktur</font>&nbsp;&nbsp;'
                    f'<font color="{P["accent_d"]}">{_p(g["structure"])}</font>', S["body"]))
                block.append(Spacer(1, 3))
            if g.get("usage_zh"):
                block.append(Paragraph(_p(g["usage_zh"]), S["body_zh"]))
                block.append(Spacer(1, 5))

            for ex in g.get("examples", []):
                block.append(Paragraph(f'· {_p(ex.get("de"))}', S["de"]))
                if ex.get("en"):
                    block.append(Paragraph(f'&nbsp;&nbsp;{_p(ex["en"])}', S["en"]))
                if ex.get("zh"):
                    block.append(Paragraph(f'&nbsp;&nbsp;{_p(ex["zh"])}', S["zh"]))
                block.append(Spacer(1, 4))

            for label, key, color in (("注意", "pitfalls_zh", P["peach"]),
                                      ("对比", "compare_zh", P["mint"])):
                if g.get(key):
                    block.append(Paragraph(
                        f'<font color="{color}">{label}</font>&nbsp;&nbsp;{_p(g[key])}', S["note"]))
            block.append(Spacer(1, 8))
            story.append(KeepTogether(block))

    # ── Teil 4：视频里的高频用法（词条形式以原文为准）──
    if analysis["phrases"]:
        story.extend(bar("Teil 4 — Wendungen", "视频里的高频用法 · 词条形式以原文为准"))
        rows = []
        for v in analysis["phrases"]:
            de = _p(v.get("de"))
            if v.get("level"):
                de += f'&nbsp;&nbsp;<font size="7" color="{P["light"]}">{_p(v["level"])}</font>'

            mean = f'{_p(v.get("en"))}<br/><font color="{P["mid"]}">{_p(v.get("zh"))}</font>'
            if v.get("usage_zh"):
                mean += f'<br/><font size="8" color="{P["light"]}">{_p(v["usage_zh"])}</font>'

            quote = f'<font color="{P["accent_d"]}">{_p(v.get("quote_de"))}</font>'
            if v.get("quote_zh"):
                quote += f'<br/><font size="8" color="{P["light"]}">{_p(v["quote_zh"])}</font>'

            alts = []
            for a in (v.get("alternatives") or []):
                line = f'<font color="{P["mint"]}">›</font> {_p(a.get("de"))}'
                if a.get("zh"):
                    line += f'&nbsp;<font size="8" color="{P["mid"]}">{_p(a["zh"])}</font>'
                if a.get("note_zh"):
                    line += f'<br/><font size="7.5" color="{P["light"]}">{_p(a["note_zh"])}</font>'
                alts.append(line)

            rows.append([
                Paragraph(de, S["cell_de"]),
                Paragraph(mean, S["cell"]),
                Paragraph(quote, S["cell"]),
                Paragraph("<br/>".join(alts) or "—", S["cell"]),
            ])
        story.append(table(["Wendung", "Bedeutung", "Im Video 原文", "还可以怎么说"],
                           rows, [36 * mm, 38 * mm, 46 * mm, 50 * mm]))

    # ── Teil 5：实用表达（原文句子 + 怎么套用到自己身上）──
    if analysis["expressions"]:
        story.extend(bar("Teil 5 — Redemittel", "原文里可复用的句子 · 怎么套到你自己身上"))
        rows = []
        for e in analysis["expressions"]:
            cat = _p(e.get("category"))
            if e.get("register"):
                cat += f'<br/><font size="7" color="{P["light"]}">{_p(e["register"])}</font>'

            quote = f'<font color="{P["accent_d"]}">{_p(e.get("de"))}</font>'
            if e.get("zh"):
                quote += f'<br/><font size="8" color="{P["light"]}">{_p(e["zh"])}</font>'

            reuse = ""
            if e.get("reuse_zh"):
                reuse += f'<font color="{P["light"]}">{_p(e["reuse_zh"])}</font><br/>'
            if e.get("adapted_de"):
                reuse += f'<font color="{P["mint"]}">→</font> {_p(e["adapted_de"])}'
            if e.get("adapted_zh"):
                reuse += f'<br/><font size="8" color="{P["light"]}">{_p(e["adapted_zh"])}</font>'

            rows.append([
                Paragraph(cat, S["cell_zh"]),
                Paragraph(quote, S["cell"]),
                Paragraph(_p(e.get("en")), S["cell"]),
                Paragraph(reuse or "—", S["cell"]),
            ])
        story.append(table(["Kategorie", "Im Video 原文", "English", "So nutzt du es 怎么套用"],
                           rows, [22 * mm, 50 * mm, 32 * mm, 66 * mm]))

    doc.build(story)
    return filepath


def safe_filename(name: str, fallback: str = "Deutsch") -> str:
    name = re.sub(r"[^\w\säöüÄÖÜß-]", "", str(name or "")).strip()
    name = re.sub(r"\s+", "_", name)
    return (name or fallback)[:60]


# ─────────────────────────────────────────────
# GUI — Minimalist Pastel Design
# ─────────────────────────────────────────────

C = {
    "bg":           "#F7F8FC",
    "card":         "#FFFFFF",
    "header_bg":    "#EEF2FF",
    "accent":       "#5C63B8",
    "accent_hover": "#4A51A0",
    "lavender":     "#C5C8F0",
    "text":         "#1A1D2E",
    "text_mid":     "#374151",
    "text_light":   "#6B7280",
    "border":       "#D1D5DB",
    "log_bg":       "#1A1D2E",
    "log_fg":       "#A5B4FC",
}

FF = "Helvetica Neue"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("German Context Builder")
        self.geometry("780x700")
        self.minsize(700, 600)
        self.configure(bg=C["bg"])

        # 和 german_vocab.py 共用同一个 config.json（共享 api_key）
        self.config_file = Path(__file__).resolve().parent / "config.json"
        self.config = self._load_config()

        self._setup_styles()
        self._build_ui()
        self._check_deps()

        # 让 core 层的重试 / 降级信息出现在界面日志里
        import german_context
        german_context.log_hook = self.log_msg

    def _load_config(self) -> dict:
        if self.config_file.exists():
            try:
                return json.loads(self.config_file.read_text())
            except Exception:
                pass
        cfg = {"api_key": "", "pdf_output_dir": str(Path.home() / "Desktop" / "German_PDF")}
        try:
            self.config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        except Exception:
            pass
        return cfg

    def _save_config(self):
        # 重新读一遍，避免覆盖 german_vocab.py 写进去的键
        disk = {}
        if self.config_file.exists():
            try:
                disk = json.loads(self.config_file.read_text())
            except Exception:
                pass
        disk["pdf_output_dir"] = self.output_dir_var.get().strip()
        disk["level"] = self.level_var.get()
        disk.setdefault("api_key", self.config.get("api_key", ""))
        self.config_file.write_text(json.dumps(disk, ensure_ascii=False, indent=2))
        self.config = disk

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Pastel.Horizontal.TProgressbar",
            troughcolor=C["border"], background=C["lavender"],
            bordercolor=C["border"], lightcolor=C["lavender"], darkcolor=C["lavender"],
        )
        style.configure("Pastel.TCombobox", fieldbackground="#F3F4F6", background="#F3F4F6")

    # ── Layout helpers ───────────────────────

    def _card(self, parent, label: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=C["card"])
        outer.pack(fill="x", pady=(0, 8))

        label_row = tk.Frame(outer, bg=C["card"], padx=16, pady=10)
        label_row.pack(fill="x")
        tk.Label(label_row, text=label.upper(), font=(FF, 9, "bold"),
                 fg=C["text_mid"], bg=C["card"]).pack(anchor="w")
        tk.Frame(outer, bg=C["border"], height=1).pack(fill="x", padx=16)

        inner = tk.Frame(outer, bg=C["card"], padx=16, pady=12)
        inner.pack(fill="x")
        return inner

    def _label(self, parent, text, width=14) -> tk.Label:
        return tk.Label(parent, text=text, width=width, anchor="w",
                        font=(FF, 11), fg=C["text"], bg=C["card"])

    def _entry(self, parent, textvariable, width=44) -> tk.Entry:
        return tk.Entry(
            parent, textvariable=textvariable, font=(FF, 11), width=width,
            bg="#F3F4F6", fg=C["text"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=C["border"],
            highlightcolor=C["accent"], insertbackground=C["accent"],
        )

    def _button(self, parent, text, command, primary=False) -> tk.Button:
        return tk.Button(
            parent, text=text,
            font=(FF, 10, "bold") if primary else (FF, 10),
            bg=C["accent"] if primary else C["border"],
            fg="white" if primary else C["text_mid"],
            activebackground=C["accent_hover"] if primary else "#C1C5CB",
            activeforeground="white" if primary else C["text"],
            relief="flat", bd=0, cursor="hand2", padx=16, pady=6, command=command,
        )

    # ── UI construction ──────────────────────

    def _build_ui(self):
        header = tk.Frame(self, bg=C["header_bg"])
        header.pack(fill="x")
        inner = tk.Frame(header, bg=C["header_bg"], padx=24, pady=18)
        inner.pack(fill="x")
        tk.Label(inner, text="German Context Builder", font=(FF, 19, "bold"),
                 fg=C["accent"], bg=C["header_bg"]).pack(anchor="w")
        tk.Label(inner,
                 text="字幕下载  ·  口语改成正确德语  ·  语法 / 词汇 / 实用表达  ·  可做笔记的 PDF",
                 font=(FF, 11), fg=C["text_mid"], bg=C["header_bg"]).pack(anchor="w", pady=(4, 0))

        tk.Frame(self, bg=C["lavender"], height=2).pack(fill="x")

        content = tk.Frame(self, bg=C["bg"])
        content.pack(fill="both", expand=True, padx=20, pady=14)

        self._build_settings(content)
        self._build_input(content)
        self._build_status(content)
        self._build_log(content)

    def _build_settings(self, parent):
        inner = self._card(parent, "Settings")

        row = tk.Frame(inner, bg=C["card"])
        row.pack(fill="x")
        self._label(row, "PDF 输出目录").pack(side="left")
        self.output_dir_var = tk.StringVar(
            value=self.config.get("pdf_output_dir", str(Path.home() / "Desktop" / "German_PDF")))
        self._entry(row, self.output_dir_var).pack(side="left", padx=(0, 10))
        self._button(row, "Browse", self._browse_dir).pack(side="left")

        row2 = tk.Frame(inner, bg=C["card"])
        row2.pack(fill="x", pady=(10, 0))
        self._label(row2, "目标水平").pack(side="left")
        self.level_var = tk.StringVar(value=self.config.get("level", "B2"))
        ttk.Combobox(row2, textvariable=self.level_var, values=["A2", "B1", "B2", "C1"],
                     width=6, state="readonly", style="Pastel.TCombobox").pack(side="left")

        self.open_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row2, text="完成后自动打开 PDF", variable=self.open_var,
                       font=(FF, 10), fg=C["text_mid"], bg=C["card"],
                       activebackground=C["card"], selectcolor=C["card"],
                       highlightthickness=0, bd=0).pack(side="left", padx=(16, 0))

    def _build_input(self, parent):
        inner = self._card(parent, "YouTube URL")

        row = tk.Frame(inner, bg=C["card"])
        row.pack(fill="x")

        self.url_var = tk.StringVar()
        url_entry = self._entry(row, self.url_var, width=52)
        url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        url_entry.bind("<Return>", lambda e: self._run())

        placeholder = "https://www.youtube.com/watch?v=..."
        url_entry.insert(0, placeholder)
        url_entry.config(fg=C["text_light"])

        def on_focus_in(e):
            if url_entry.get() == placeholder:
                url_entry.delete(0, "end")
                url_entry.config(fg=C["text"])

        def on_focus_out(e):
            if not url_entry.get():
                url_entry.insert(0, placeholder)
                url_entry.config(fg=C["text_light"])

        url_entry.bind("<FocusIn>", on_focus_in)
        url_entry.bind("<FocusOut>", on_focus_out)
        self._placeholder = placeholder

        self.run_btn = self._button(row, "▶  Build PDF", self._run, primary=True)
        self.run_btn.pack(side="left")

    def _build_status(self, parent):
        frame = tk.Frame(parent, bg=C["bg"])
        frame.pack(fill="x", pady=(0, 8))

        self.progress_bar = ttk.Progressbar(frame, mode="indeterminate",
                                            style="Pastel.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(0, 4))

        self.progress_var = tk.StringVar(value="Ready")
        tk.Label(frame, textvariable=self.progress_var, font=(FF, 10),
                 fg=C["text_mid"], bg=C["bg"]).pack(anchor="w")

    def _build_log(self, parent):
        outer = tk.Frame(parent, bg=C["card"])
        outer.pack(fill="both", expand=True)

        label_row = tk.Frame(outer, bg=C["card"], padx=16, pady=10)
        label_row.pack(fill="x")
        tk.Label(label_row, text="ACTIVITY LOG", font=(FF, 9, "bold"),
                 fg=C["text_mid"], bg=C["card"]).pack(anchor="w")
        tk.Frame(outer, bg=C["border"], height=1).pack(fill="x", padx=16)

        log_inner = tk.Frame(outer, bg=C["card"], padx=16, pady=12)
        log_inner.pack(fill="both", expand=True)

        self.log = scrolledtext.ScrolledText(
            log_inner, height=11, font=("Menlo", 11),
            bg=C["log_bg"], fg=C["log_fg"], insertbackground="white",
            state="disabled", relief="flat", bd=0, padx=12, pady=10,
        )
        self.log.pack(fill="both", expand=True)

    # ── Actions ──────────────────────────────

    def _browse_dir(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.home()))
        if path:
            self.output_dir_var.set(path)

    def _check_deps(self):
        missing = check_and_import()
        if missing:
            self.log_msg(f"缺少依赖 —— 请运行: pip install {' '.join(missing)}")
        self.log_msg("Ready  ·  贴一个 YouTube 链接，然后点 Build PDF")
        if not (GEMINI_API_KEY or self.config.get("api_key")):
            self.log_msg(f"⚠  没有找到 API Key —— 请打开 {self.config_file} 填入 api_key")

    def log_msg(self, msg: str):
        def write():
            self.log.configure(state="normal")
            self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}]  {msg}\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, write)

    def _set_status(self, msg: str):
        self.log_msg(msg)
        self.after(0, lambda: self.progress_var.set(msg))

    def _run(self):
        url = self.url_var.get().strip()
        if url == self._placeholder:
            url = ""

        api_key = GEMINI_API_KEY or self.config.get("api_key", "")
        out_dir = self.output_dir_var.get().strip()

        if not url:
            messagebox.showwarning("缺少链接", "请先贴一个 YouTube 链接。")
            return
        if not api_key:
            messagebox.showwarning(
                "缺少 API Key",
                f"请用文本编辑器打开下面这个文件，把 Gemini API Key 填进 api_key：\n\n{self.config_file}")
            return
        if not out_dir:
            messagebox.showwarning("缺少目录", "请先选择 PDF 的输出目录。")
            return

        self._save_config()
        self.run_btn.configure(state="disabled", bg=C["text_light"])
        self.progress_bar.start(10)

        # tkinter 变量在主线程读好再传给 worker，避免跨线程访问
        threading.Thread(
            target=self._worker,
            args=(url, api_key, out_dir, self.level_var.get(), bool(self.open_var.get())),
            daemon=True).start()

    def _worker(self, url: str, api_key: str, out_dir: str, level: str, auto_open: bool):
        try:
            os.makedirs(out_dir, exist_ok=True)

            # 1 ── 字幕
            self._set_status("解析视频 ID …")
            video_id = extract_video_id(url)
            if not video_id:
                raise ValueError("看不懂这个链接，请检查格式。")
            self.log_msg(f"Video ID: {video_id}")

            self._set_status("下载字幕 …")
            transcript, lang = get_transcript(video_id)
            if len(transcript) > MAX_TRANSCRIPT:
                transcript = transcript[:MAX_TRANSCRIPT]
                self.log_msg(f"字幕过长，只处理前 {MAX_TRANSCRIPT:,} 字符")
            self.log_msg(f"字幕语言: {lang}  ·  {len(transcript):,} 字符")
            if not lang.lower().startswith("de"):
                self.log_msg("⚠  这条字幕不是德语，结果可能不理想")

            # 2 ── 口语 → 标准德语
            n_chunks = len(chunk_text(transcript))
            self._set_status(f"整理字幕 + 翻译 …  共 {n_chunks} 块")
            cache_file = Path(out_dir) / ".cache" / f"{video_id}.correction.json"
            corrected = correct_transcript(
                transcript, api_key,
                progress=lambda i, n: self._set_status(f"整理中 …  {i}/{n}"),
                cache_file=cache_file)
            zh_ok = sum(1 for x in corrected["segments"] if x.get("zh"))
            self.log_msg(f"字幕整理完成 · {len(corrected['segments'])} 段 · "
                         f"中文翻译 {zh_ok} 段")

            full_text = "\n".join(corrected["segments"])

            # 3 ── 语法 / 词汇 / 实用表达
            self._set_status(f"整理语法、词汇和实用表达  (目标 {level}) …")
            analysis = analyze_transcript(full_text, api_key, level)
            self.log_msg(f"语法点 {len(analysis['grammar_points'])} · "
                         f"高频用法 {len(analysis['phrases'])} · "
                         f"实用表达 {len(analysis['expressions'])}")

            # 4 ── PDF
            self._set_status("生成 PDF …")
            title = analysis.get("title") or "Deutsch"
            filename = f"{safe_filename(title)}_{video_id}.pdf"
            path = os.path.join(out_dir, filename)
            build_pdf(corrected, analysis,
                      {"url": url, "lang": lang, "level": level,
                       "footer": f"{title} · {video_id}"},
                      path)
            self.log_msg(f"已保存 → {path}")

            # 全流程跑通了，进度缓存就没用了
            try:
                cache_file.unlink(missing_ok=True)
            except Exception:
                pass

            self.log_msg("─" * 52)
            for g in analysis["grammar_points"][:5]:
                self.log_msg(f"  语法  {g.get('name','')}")
            for e in analysis["expressions"][:5]:
                self.log_msg(f"  表达  {e.get('de','')}")
            self.log_msg("─" * 52)

            self._set_status(f"完成  ·  {filename}")
            self.after(0, lambda: self.url_var.set(""))

            if auto_open:
                try:
                    if sys.platform == "darwin":
                        subprocess.run(["open", path], check=False)
                    elif os.name == "nt":
                        os.startfile(path)            # type: ignore[attr-defined]
                    else:
                        subprocess.run(["xdg-open", path], check=False)
                except Exception:
                    pass

        except Exception as e:
            self.log_msg(f"Error: {e}")
            self._set_status(f"失败: {str(e)[:70]}")
        finally:
            self.after(0, lambda: self.run_btn.configure(state="normal", bg=C["accent"]))
            self.after(0, self.progress_bar.stop)


if __name__ == "__main__":
    App().mainloop()
