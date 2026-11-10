#!/usr/bin/env python3
"""UGC Ad Demo: product brief -> scripts -> policy check -> pictures, voice, video -> summary page.

Run `python3 make_ads.py` for a free dry run. Settings live in config.toml.
Only Python's built-in libraries are used, so there is nothing to install.
"""
import argparse
import base64
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USER_AGENT = "ugc-ad-demo/1.0"

# Typical Claude usage per run. Only used to estimate the cost of a full run from a dry run.
TYPICAL_SCRIPT_TOKENS = (1500, 3000)   # (input, output)
TYPICAL_POLICY_TOKENS = (900, 250)     # per variation

REQUIRED_FIELDS = ["angle", "hook_type", "persona", "format", "hook", "spoken_lines", "scenes", "on_screen_text", "cta"]


# ----------------------------------------------------------------------------
# Small helpers: settings, keys, web calls
# ----------------------------------------------------------------------------
def load_env(path=ROOT / ".env"):
    """Read KEY=VALUE lines from .env into the environment (no extra package needed)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need_key(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name}. Add it to your .env file (see .env.example), or run in dry mode.")
    return value


def deep_merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(showcase=False):
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    if showcase:
        cfg = deep_merge(cfg, cfg.get("showcase", {}))
    return cfg


def http(url, data=None, headers=None, method=None, timeout=120):
    """One place for every web call. Returns (body_bytes, content_type)."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        body = err.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {err.code} from {url.split('?')[0]}: {body}") from None
    except urllib.error.URLError as err:
        raise RuntimeError(f"Could not reach {url.split('/')[2]}: {err.reason}") from None


def http_json(url, payload=None, headers=None, method=None, timeout=120):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    body, _ = http(url, data=data, headers=hdrs, method=method, timeout=timeout)
    return json.loads(body)


def extract_json(text):
    """Pull the first JSON object out of a model reply (handles code fences and extra words)."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON found in the model reply.")
    return json.loads(text[start:end + 1])


# ----------------------------------------------------------------------------
# Cost tracking (US dollars)
# ----------------------------------------------------------------------------
class Costs:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lines = []          # what this run really spent
        self.claude_usd = 0.0
        self.debug_dir = None    # where to save a Claude reply that could not be read

    def add(self, item, detail, usd):
        self.lines.append({"item": item, "detail": detail, "usd": round(usd, 4)})

    def claude(self, model, tokens_in, tokens_out, label):
        usd = token_cost(self.cfg, model, tokens_in, tokens_out)
        self.claude_usd += usd
        self.add(f"Claude: {label}", f"{model}, {tokens_in} in / {tokens_out} out tokens", usd)

    def load_previous(self, costs_file):
        """Carry over what an earlier part of this run already spent, so the total stays honest."""
        if costs_file.exists():
            self.lines = json.loads(costs_file.read_text(encoding="utf-8")).get("lines", [])
            self.claude_usd = sum(line["usd"] for line in self.lines if line["item"].startswith("Claude"))

    @property
    def spent(self):
        return round(sum(line["usd"] for line in self.lines), 4)


def token_cost(cfg, model, tokens_in, tokens_out):
    price = cfg.get("prices", {}).get(model)
    if not price:
        return 0.0
    return tokens_in / 1e6 * price["input"] + tokens_out / 1e6 * price["output"]


def video_seconds(cfg, variation):
    words = len(variation["spoken_lines"].split())
    return max(3.0, words / cfg["video"]["words_per_second"])


def wants_video(cfg, variation, video_ids):
    return cfg["video"]["enabled"] or variation["id"] in video_ids


def estimate_full(cfg, variations, costs, mode, video_ids):
    """What a full run of these variations would cost with the current settings."""
    n = len(variations)
    c = cfg["claude"]
    if mode == "dry":
        claude = token_cost(cfg, c["script_model"], *TYPICAL_SCRIPT_TOKENS) \
            + n * token_cost(cfg, c["policy_model"], *TYPICAL_POLICY_TOKENS)
    else:
        claude = costs.claude_usd
    images = n * cfg["image"]["usd_per_image"]
    voice = 0.0
    if cfg["voice"]["enabled"]:
        chars = sum(len(v["spoken_lines"]) for v in variations)
        voice = chars / 1000 * cfg["voice"]["usd_per_1000_characters"]
    video = sum(video_seconds(cfg, v) * cfg["video"]["usd_per_second"]
                for v in variations if wants_video(cfg, v, video_ids))
    each_video = sum(video_seconds(cfg, v) for v in variations) / n * cfg["video"]["usd_per_second"]
    parts = {"claude": claude, "images": images, "voice": voice, "video": video}
    return {
        "parts_usd": {k: round(v, 3) for k, v in parts.items()},
        "total_usd": round(sum(parts.values()), 2),
        "video_per_ad_usd": round(each_video, 2),
    }


# ----------------------------------------------------------------------------
# Claude call
# ----------------------------------------------------------------------------
def call_claude(cfg, costs, model, system, user, max_tokens, label):
    out = http_json(
        "https://api.anthropic.com/v1/messages",
        {"model": model, "max_tokens": max_tokens, "system": system,
         "messages": [{"role": "user", "content": user}]},
        {"x-api-key": need_key("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
        timeout=180,
    )
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    usage = out.get("usage", {})
    costs.claude(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0), label)
    if out.get("stop_reason") == "max_tokens":
        raise RuntimeError(f"Claude's reply for '{label}' was cut off. Raise its max_tokens setting in config.toml.")
    return text


def claude_json(cfg, costs, model, system, user, max_tokens, label):
    """Ask Claude for JSON. If the reply cannot be read, show it back to Claude once and ask for a fixed copy."""
    text = call_claude(cfg, costs, model, system, user, max_tokens, label)
    try:
        return extract_json(text)
    except ValueError as err:       # includes json.JSONDecodeError
        if costs.debug_dir:
            slug = re.sub(r"\W+", "_", label)
            (costs.debug_dir / f"debug_{slug}.txt").write_text(text, encoding="utf-8")
        fix = (f"{user}\n\nYour previous reply was not valid JSON ({err}). Here it was:\n{text}\n\n"
               "Reply again with the same content as valid JSON only. "
               "Do not use double quotes inside text values; use single quotes instead.")
        return extract_json(call_claude(cfg, costs, model, system, fix, max_tokens, f"{label} (retry)"))


# ----------------------------------------------------------------------------
# Step 1: angles, hooks and scripts
# ----------------------------------------------------------------------------
SCRIPT_SYSTEM = """You are a senior performance-marketing creative strategist. You write short UGC-style video ads \
for Facebook and Instagram. Reply with JSON only. No extra words."""

SCRIPT_RULES = """- Write spoken_lines the way a real person talks into their phone camera, NOT like ad copy. Use contractions, short \
sentences and fragments, and one small human touch such as 'okay so', 'honestly', 'wait', or a quick self-correction. \
Use '...' for a short pause. Never use marketing words like seamless, powerful, effortless or revolutionary. \
No stage directions or brackets inside spoken_lines. 35 to 50 words, one speaker, plain words.
- The speaker is a fictional character. Never say or imply the speaker is a real customer or a real expert.
- Respect every item in "Must NOT claim". No fear claims, no guarantees, no before/after, no fake numbers.
- Calm, helpful tone. The hook must work in the first 2 seconds.
- Avoid absolute words about people: nobody, everyone, always, never. Say 'many people' or 'most people' instead.
- Inside text values use only single quotes ('like this'). Never use double quotes inside a value."""

SCRIPT_SHAPE = """{"variations": [{
  "id": 1,
  "angle": "the idea in one short line",
  "hook_type": "e.g. Question, Story, Myth-busting, Demo, Relatable moment",
  "persona": "who is speaking, e.g. busy mom in her 40s",
  "persona_look": "short picture description of the person (age range, hair, clothes). Fictional.",
  "setting": "where the video is shot, e.g. a bright home kitchen",
  "voice": "female or male",
  "voice_age": "young (under 30), middle (30 to 55) or older (over 55). Must match the persona.",
  "format": "e.g. Selfie talk-to-camera, Walk-and-talk, Phone screen demo",
  "hook": "the first spoken sentence",
  "spoken_lines": "the full spoken script",
  "scenes": ["scene 1", "scene 2", "scene 3"],
  "on_screen_text": ["text 1", "text 2", "text 3"],
  "cta": "call to action"
}]}"""

SCRIPT_USER = """Product brief:
---
{brief}
---

Write {n} different ad variations. Each one must differ in hook type, persona and format.
Rules:
{rules}

Return exactly this JSON shape:
{shape}"""

VARIATION_USER = """Product brief:
---
{brief}
---

This ad works well. Here it is as JSON:
{base}

Write {n} new versions of this ad. In every version change exactly ONE thing, and only this:
{change}

Keep everything else the same as the original: the idea, hook type, persona, look, setting, voice, format, scenes, on-screen text and call to action. Reuse the original wording wherever the change does not force new wording. The {n} versions must be different takes on that one change, not copies of each other.
Rules:
{rules}

Return exactly this JSON shape (one extra field, "changed"):
{shape_changed}"""


def clean_variation(raw, index):
    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ValueError(f"Variation {index} is missing: {', '.join(missing)}")
    v = dict(raw)
    v["id"] = index
    v["scenes"] = [str(s) for s in v["scenes"]]
    v["on_screen_text"] = [str(s) for s in v["on_screen_text"]]
    v.setdefault("persona_look", f"a friendly person ({v['persona']})")
    v.setdefault("setting", "a bright, ordinary room at home")
    v["voice"] = "male" if str(v.get("voice", "")).lower().startswith("m") else "female"
    age = str(v.get("voice_age", "")).lower()
    v["voice_age"] = "young" if age.startswith("y") else "older" if age.startswith("o") else "middle"
    return v


def step_scripts(cfg, costs, brief, mode, n):
    if mode == "dry":
        data = json.loads((ROOT / "samples" / "sample_variations.json").read_text(encoding="utf-8"))
        raw = data["variations"]
    else:
        data = claude_json(cfg, costs, cfg["claude"]["script_model"], SCRIPT_SYSTEM,
                           SCRIPT_USER.format(brief=brief, n=n, rules=SCRIPT_RULES, shape=SCRIPT_SHAPE), cfg["claude"]["script_max_tokens"], "scripts")
        raw = data["variations"]
    return [clean_variation(r, i) for i, r in enumerate(raw, start=1)]


AVATAR_FIELDS = ["persona", "persona_look", "setting", "voice", "voice_age"]


def step_variations(cfg, costs, brief, mode, base, change, n):
    """New versions of one ad. Each changes only what the user asked for."""
    if mode == "dry":
        raw = []
        for k in range(1, n + 1):
            v = dict(base)
            v["angle"] = f"{base['angle']} (dry sample {k})"
            v["changed"] = f"Dry run, no Claude call. Real runs would change: {change}"
            raw.append(v)
    else:
        shape = SCRIPT_SHAPE.replace('"cta": "call to action"', '"cta": "call to action",\n  "changed": "one short line: what is different from the original"')
        user = VARIATION_USER.format(brief=brief, base=json.dumps(
            {k: v for k, v in base.items() if k != "id"}, indent=2, ensure_ascii=False), n=n, change=change,
            rules=SCRIPT_RULES, shape_changed=shape)
        raw = claude_json(cfg, costs, cfg["claude"]["script_model"], SCRIPT_SYSTEM, user,
                          cfg["claude"]["script_max_tokens"], "variations")["variations"][:n]
    out = [clean_variation(r, i) for i, r in enumerate(raw, start=1)]
    for v in out:
        v["changed"] = str(v.get("changed") or change)
    return out


def base_voice_id(cfg, src_run, base_variations, base_id):
    """The ElevenLabs voice an earlier ad used: saved in its folder, or worked out the way the run picked it."""
    saved = src_run / f"ad_{base_id}" / "voice_id.txt"
    if saved.exists():
        return saved.read_text(encoding="utf-8").strip()
    used, found = {}, None
    for bv in base_variations:
        vid = pick_voice(cfg, build_prompts(bv, cfg), used)
        if bv["id"] == base_id:
            found = vid
    return found


# ----------------------------------------------------------------------------
# Step 2: policy check (quick local rules + Claude Haiku)
# ----------------------------------------------------------------------------
# These catch the obvious problems for free. They are a first filter, not legal advice.
def build_rules(product):
    p = re.escape(product.lower())
    return [
        ("Fear-based claim", r"\bhacked\b|\bcompromised\b|\bbeing (?:watched|spied on|tracked)\b|\bsomeone is (?:watching|spying)\b|\byou(?:'ve| have) been scammed\b",
         "Tells the viewer something bad is happening to them. Meta limits fear-based and personal-attribute claims."),
        ("Guarantee or absolute claim", r"\bguarantee[sd]?\b|\b100\s?%|\bcompletely (?:safe|secure|protected)\b|\bunhackable\b|\bnever (?:get|be|fall for)\b|\bstops? (?:all|every) scams?\b|\brisk[- ]free\b",
         "Promises a result nobody can promise. Breaks the brief's 'must not claim' list."),
        ("Before-and-after claim", rf"\bbefore and after\b|\bbefore\s*(?:vs\.?|versus|/)\s*after\b|\b(?:before|after) (?:using )?(?:{p}|this app|the app)\b",
         "Before-and-after comparisons are restricted by Meta and cannot be proven here."),
        ("Fake testimonial or expert claim", r"\b(?:real|actual|verified) (?:customer|user|review)\b|\bas a (?:real|paying) customer\b|\bI(?: am|'m) (?:a |an )?(?:doctor|nurse|lawyer|police officer|bank manager|cybersecurity expert)\b",
         "Presents an AI character as a real customer or expert."),
        ("Endorsement claim", r"\bendorsed by\b|\bapproved by\b|\brecommended by\b|\bas seen on\b|\bpartner(?:ed)? with\b",
         "Claims backing from a named group. Not in the brief."),
        ("Made-up statistic", r"\b\d{2,3}\s?(?:%|percent) of (?:people|users|families|scams|adults)\b",
         "A number with no source."),
    ]


def ad_text(v):
    return " ".join([v["hook"], v["spoken_lines"], v["cta"], *v["on_screen_text"], *v["scenes"]])


def product_name(brief):
    m = re.search(r"^#\s*Product brief:\s*([^\(\n]+)", brief, re.M)
    return m.group(1).strip() if m else "the product"


def local_flags(v, product):
    text = ad_text(v)
    flags, seen = [], set()
    for category, pattern, why in build_rules(product):
        for m in re.finditer(pattern, text, re.I):
            key = (category, m.group(0).lower())
            if key not in seen:          # the same phrase in the script and on screen is one issue
                seen.add(key)
                flags.append({"source": "rules", "severity": "fix", "category": category, "quote": m.group(0), "why": why})
    return flags


POLICY_SYSTEM = """You review ad scripts against Meta (Facebook and Instagram) advertising policies and the brand brief. \
Reply with JSON only.

Context: every ad is a dramatised scene with a fictional character, and it is published with a visible "AI-generated" label. \
A fictional character telling a short everyday story is normal for UGC-style ads. It is NOT a problem by itself.

Mark an issue "fix" (must change before running) when you find:
- fear-based claims, or saying the viewer's own phone, account or data is hacked, exposed or at risk
- guarantees, or absolute words about people or results (nobody, everyone, always, never, 100%)
- before-and-after claims
- the character saying they are a real customer, expert or professional, or claiming a specific result
- made-up statistics, endorsements, or anything in the brief's "Must NOT claim" list
- stating or implying something about the viewer's health, money or identity

Mark an issue "tip" (optional improvement, the ad can still run) for softer things, such as wording that could sound \
preachy or a line that could be clearer.

Do NOT flag: a plain description of the product's real features from the brief, a question about the viewer's family or \
messages, or a reaction shot of a character. Only flag what is really in the script."""

POLICY_USER = """Brief:
---
{brief}
---
Ad script to review:
Spoken lines: {spoken}
On-screen text: {onscreen}
Scenes: {scenes}
Call to action: {cta}

Return exactly: {{"verdict": "pass" or "review", "issues": [{{"severity": "fix" or "tip", "category": "...", "quote": "exact words from the script", "why": "one plain sentence", "fix": "a safer wording"}}]}}
Use "review" if there is at least one "fix" issue. Otherwise use "pass". Maximum 6 issues."""


def step_policy(cfg, costs, brief, variations, mode):
    product = product_name(brief)
    strict = cfg.get("policy", {}).get("strict", False)   # strict = tips also change the verdict to review
    report = []
    for v in variations:
        issues = local_flags(v, product)
        checked_by = ["local rules"]
        if mode != "dry":
            try:
                result = claude_json(
                    cfg, costs, cfg["claude"]["policy_model"], POLICY_SYSTEM,
                    POLICY_USER.format(brief=brief, spoken=v["spoken_lines"], onscreen=" | ".join(v["on_screen_text"]),
                                       scenes=" | ".join(v["scenes"]), cta=v["cta"]),
                    cfg["claude"]["policy_max_tokens"], f"policy check, ad {v['id']}")
                for item in result.get("issues", []):
                    severity = item.get("severity", "fix")
                    issues.append({"source": "claude", "severity": severity if severity in ("fix", "tip") else "fix",
                                   "category": item.get("category", "other"), "quote": item.get("quote", ""),
                                   "why": item.get("why", ""), "fix": item.get("fix", "")})
                if result.get("verdict") == "review" and not any(i["severity"] == "fix" for i in issues):
                    issues.append({"source": "claude", "severity": "fix", "category": "other", "quote": "",
                                   "why": "Claude asked for a human review.", "fix": ""})
                checked_by.append(cfg["claude"]["policy_model"])
            except Exception as err:   # a failed check must never look like a pass
                issues.append({"source": "claude", "severity": "fix", "category": "check-error", "quote": "",
                               "why": f"The Claude check failed ({err}). Treat as needs review.", "fix": ""})
        needs_review = any(i["severity"] == "fix" or strict for i in issues)
        report.append({"id": v["id"], "verdict": "review" if needs_review else "pass", "checked_by": checked_by, "issues": issues})
    return report


# ----------------------------------------------------------------------------
# Step 3: pictures, voice, video
# ----------------------------------------------------------------------------
def build_prompts(v, cfg):
    image = (f"Vertical 9:16 smartphone selfie-style photo of {v['persona_look']}, in {v['setting']}. "
             "Looking straight at the camera with a natural, friendly expression, holding the phone at arm's length. "
             "Casual user-generated-content look, soft natural light, slightly imperfect framing, realistic skin. "
             "No text, no logos, no watermark. A fictional person.")
    video = ("The person talks naturally to the camera, with small head and hand movements and a friendly face. "
             "Steady handheld phone feel. Keep the same person and background.")
    return {"image_prompt": image, "video_prompt": video, "voice_text": v["spoken_lines"],
            "voice": v["voice"], "voice_age": v["voice_age"]}


def as_list(value):
    return [value] if isinstance(value, str) else list(value)


def pick_voice(cfg, prompts, used=None):
    """Voice for this gender and age group. A slot can hold one ID or a list: ads that share a slot take turns.
    If the slot is empty, the plain male or female voice is used."""
    ids = cfg["voice"]["ids"]
    slot = f"{prompts['voice']}_{prompts['voice_age']}"
    options = [i for i in as_list(ids.get(slot, [])) if i] or as_list(ids[prompts["voice"]])
    used = used if used is not None else {}
    turn = used.get(slot, 0)
    used[slot] = turn + 1
    return options[turn % len(options)]


def tts(cfg, voice_id, text):
    """Turn text into speech with ElevenLabs. Returns the mp3 bytes."""
    payload = {"text": text, "model_id": cfg["voice"]["model_id"]}
    if cfg["voice"].get("settings"):
        payload["voice_settings"] = cfg["voice"]["settings"]
    audio, _ = http(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", json.dumps(payload).encode("utf-8"),
        {"xi-api-key": need_key("ELEVENLABS_API_KEY"), "Content-Type": "application/json", "Accept": "audio/mpeg"},
        timeout=180)
    return audio


def find_url(node):
    """Find the first media URL in a model reply, whatever the model calls its fields."""
    if isinstance(node, dict):
        if isinstance(node.get("url"), str) and node["url"].startswith("http"):
            return node["url"]
        for value in node.values():
            found = find_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_url(value)
            if found:
                return found
    return None


def fal_run(model, payload, timeout_s=900):
    """Send a job to fal.ai, wait for it, return the result. Works the same for every fal model."""
    headers = {"Authorization": f"Key {need_key('FAL_KEY')}"}
    job = http_json(f"https://queue.fal.run/{model}", payload, headers)
    deadline = time.time() + timeout_s
    while True:
        status = http_json(job["status_url"], headers=headers, method="GET")
        state = status.get("status")
        if state == "COMPLETED":
            return http_json(job["response_url"], headers=headers, method="GET")
        if state in ("FAILED", "ERROR", "CANCELLED"):
            raise RuntimeError(f"fal.ai job for {model} ended with status {state}")
        if time.time() > deadline:
            raise TimeoutError(f"fal.ai job for {model} took longer than {timeout_s} seconds")
        time.sleep(3)


EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}


def download(url, folder, stem):
    body, ctype = http(url, timeout=300)
    ext = EXTENSIONS.get(ctype.split(";")[0].strip(), Path(url.split("?")[0]).suffix or ".bin")
    path = folder / f"{stem}{ext}"
    path.write_bytes(body)
    return path


def placeholder_svg(v, folder):
    lines = ["PLACEHOLDER", "(dry run: no image made)", ""] + textwrap.wrap(v["persona"], 28) + [""] + textwrap.wrap(v["setting"], 28)
    text = "".join(f'<text x="270" y="{340 + i * 40}" text-anchor="middle" font-family="Arial" font-size="26" fill="#475569">{html.escape(t)}</text>'
                   for i, t in enumerate(lines))
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="540" height="960" viewBox="0 0 540 960">'
           '<rect width="540" height="960" fill="#e2e8f0"/><rect x="20" y="20" width="500" height="920" rx="24" fill="none" stroke="#94a3b8" stroke-width="3" stroke-dasharray="12 10"/>'
           f"{text}</svg>")
    path = folder / "image.svg"
    path.write_text(svg, encoding="utf-8")
    return path


def silent_wav(folder, seconds):
    path = folder / "voice.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def existing_image(folder):
    """A real picture already saved in this ad's folder (the placeholder .svg does not count)."""
    return next((p for ext in IMAGE_TYPES for p in [folder / f"image{ext}"] if p.exists()), None)


def make_assets(cfg, costs, v, prompts, folder, mode, want_video, voice_use=None, redo=()):
    """Returns a dict of what was made for one variation.
    Real files that already exist in the folder are kept, so a resumed run only makes what is missing.
    `redo` can hold "image", "voice" or "video" to make that piece again."""
    folder.mkdir(parents=True, exist_ok=True)
    out = {"image": None, "voice": None, "video": None, "notes": []}
    secs = video_seconds(cfg, v)

    have_image = None if "image" in redo else existing_image(folder)
    have_voice = None if "voice" in redo or not (folder / "voice.mp3").exists() else folder / "voice.mp3"
    have_video = None if "video" in redo or not (folder / "video.mp4").exists() else folder / "video.mp4"
    if mode == "full" and (("image" in redo) or ("voice" in redo)) and (folder / "video.mp4").exists():
        (folder / "video.mp4").replace(folder / "video_previous.mp4")   # it was made from the old picture or voice
        have_video = None
        out["notes"].append("The old video no longer matches the new picture or voice. It was kept as video_previous.mp4.")

    # Picture
    if have_image:
        out["image"] = have_image
    elif mode == "full":
        try:
            payload = dict(cfg["image"].get("params", {}), prompt=prompts["image_prompt"])
            result = fal_run(cfg["image"]["model"], payload)
            out["image_url"] = find_url(result)
            out["image"] = download(out["image_url"], folder, "image")
            costs.add(f"Image, ad {v['id']}", cfg["image"]["model"], cfg["image"]["usd_per_image"])
        except Exception as err:
            out["notes"].append(f"Image failed: {err}. Showing a placeholder instead.")
    if out["image"] is None:
        out["image"] = placeholder_svg(v, folder)

    # Voice
    if cfg["voice"]["enabled"]:
        if have_voice:
            out["voice"] = have_voice
        elif mode == "full":
            try:
                voice_id = prompts.get("voice_id") or pick_voice(cfg, prompts, voice_use)
                audio = tts(cfg, voice_id, prompts["voice_text"])
                out["voice"] = folder / "voice.mp3"
                out["voice"].write_bytes(audio)
                (folder / "voice_id.txt").write_text(voice_id, encoding="utf-8")
                costs.add(f"Voice, ad {v['id']}", f"ElevenLabs, {len(prompts['voice_text'])} characters (rough)",
                          len(prompts["voice_text"]) / 1000 * cfg["voice"]["usd_per_1000_characters"])
            except Exception as err:
                out["notes"].append(f"Voice failed: {err}. Showing a silent placeholder instead.")
        if out["voice"] is None:
            out["voice"] = silent_wav(folder, min(secs, 20))

    # Video (only when asked for)
    if have_video:
        out["video"] = have_video
    elif want_video:
        real_image = out["image"].suffix in IMAGE_TYPES
        real_voice = bool(out["voice"]) and out["voice"].suffix == ".mp3"
        if mode != "full":
            out["notes"].append("Video skipped (only runs in full mode).")
        elif not (real_image and real_voice):
            out["notes"].append("Video skipped because the real picture or voice is missing.")
        else:
            try:
                image_url = out.get("image_url") or (
                    f"data:{IMAGE_TYPES[out['image'].suffix]};base64," + base64.b64encode(out["image"].read_bytes()).decode("ascii"))
                audio_uri = "data:audio/mpeg;base64," + base64.b64encode(out["voice"].read_bytes()).decode("ascii")
                result = fal_run(cfg["video"]["model"], {"image_url": image_url, "audio_url": audio_uri,
                                                         "prompt": prompts["video_prompt"]})
                out["video"] = download(find_url(result), folder, "video")
                costs.add(f"Video, ad {v['id']}", f"{cfg['video']['model']}, about {secs:.0f} seconds",
                          secs * cfg["video"]["usd_per_second"])
            except Exception as err:
                out["notes"].append(f"Video failed: {err}.")
    return out


# ----------------------------------------------------------------------------
# Step 4: summary page
# ----------------------------------------------------------------------------
CSS = """
:root{--bg:#f8fafc;--card:#fff;--ink:#0f172a;--mute:#64748b;--line:#e2e8f0;--ok:#15803d;--okbg:#dcfce7;--warn:#b45309;--warnbg:#fef3c7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.55 system-ui,Segoe UI,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 18px 60px}h1{margin:0 0 4px;font-size:32px}h2{font-size:22px;margin:28px 0 12px}
.sub{color:var(--mute);margin:0 0 16px}.banner{background:#e0f2fe;border:1px solid #bae6fd;border-radius:10px;padding:10px 14px;margin:14px 0;font-size:16px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:16px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{background:#f1f5f9;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:14px}.pass{background:var(--okbg);color:var(--ok)}.review{background:var(--warnbg);color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(480px,100%),1fr));gap:18px}.scroll{overflow-x:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;gap:16px;flex-wrap:wrap}
.media{width:170px;flex:none}.media img,.media video{width:170px;aspect-ratio:9/16;object-fit:cover;border-radius:10px;background:#e2e8f0;display:block}
.media.big,.media.big video,.media.big audio{width:min(340px,100%)}.media a{font-size:15px;display:inline-block;margin-top:6px}
.media audio{width:170px;margin-top:8px}.tag{display:block;font-size:13px;color:var(--mute);margin-top:4px}
.body{flex:1;min-width:220px}.body h3{margin:0 0 2px;font-size:19px}.meta{color:var(--mute);font-size:15px;margin-bottom:8px}
.say{background:#f1f5f9;border-radius:8px;padding:8px 10px;font-size:16px;margin:8px 0}ul{margin:4px 0 8px 18px;padding:0;font-size:16px}
.issue{background:var(--warnbg);border-radius:8px;padding:6px 10px;margin:6px 0;font-size:15px}.issue.tip{background:#eff6ff}.note{color:var(--warn);font-size:15px}
.costbox{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}.costbox div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.costbox b{display:block;font-size:26px}.small{font-size:15px;color:var(--mute)}
"""


def usd(x):
    return f"${x:,.2f}"


def changed_html(v):
    return f'<div class="meta"><b>What changed:</b> {html.escape(v["changed"])}</div>' if v.get("changed") else ""


def next_step_html(mode, run_dir, assets, estimate):
    if mode != "full" or all(a["video"] for a in assets.values()):
        return ""
    ids = " ".join(str(i) for i, a in assets.items() if not a["video"])
    return ('<div class="banner"><b>Approve first, then make video.</b> Read the lines and listen to the voices. '
            f'Then make a video only for the ads you like (about {usd(estimate["video_per_ad_usd"])} each). '
            f'For example: <code>python3 make_ads.py --resume {html.escape(run_dir.name)} --video-for {ids.split()[0]}</code></div>')


def origin_html(origin):
    if not origin:
        return ""
    src = html.escape(origin["from_run"])
    return (f'<div class="banner"><b>Versions of ad {origin["base_ad"]}</b> from the original results page '
            f'(<a href="../{src}/summary.html">{src}</a>). '
            f'Only one thing changed: {html.escape(origin["change"])}</div>')


def issue_html(i):
    e = html.escape
    tip = i.get("severity") == "tip"
    out = (f'<div class="issue{" tip" if tip else ""}"><b>{"Tip: " if tip else ""}{e(i["category"])}</b>: '
           f'&ldquo;{e(i["quote"])}&rdquo;<br>{e(i["why"])}')
    if i.get("fix"):
        out += f'<br><i>Safer:</i> {e(i["fix"])}'
    return out + "</div>"


def build_summary(cfg, product, mode, run_dir, variations, report, assets, prompts, costs, estimate, origin=None):
    e = html.escape
    label = e(cfg["labels"]["ai_label"])
    rows, cards = [], []
    for v in variations:
        rep = next(r for r in report if r["id"] == v["id"])
        a = assets[v["id"]]
        badge = f'<span class="badge {rep["verdict"]}">{"PASS" if rep["verdict"] == "pass" else "REVIEW"}</span>'
        tips = sum(1 for i in rep["issues"] if i.get("severity") == "tip")
        tip_note = f' <span class="small">{tips} tip{"s" if tips != 1 else ""}</span>' if tips else ""
        change_cell = (f"<td>{e(v['changed'])}<br><span class=\"small\">&ldquo;{e(v['hook'])}&rdquo;</span></td>"
                       if origin else "")
        rows.append(f"<tr><td>{v['id']}</td><td>{e(v['hook_type'])}</td><td>{e(v['persona'])}</td><td>{e(v['format'])}</td><td>{badge}{tip_note}</td>{change_cell}</tr>")
        rel = lambda p: e(str(Path(p).relative_to(run_dir)).replace("\\", "/"))
        media = f'<img src="{rel(a["image"])}" alt="Ad {v["id"]} picture">' if not a["video"] else \
            f'<video controls playsinline preload="metadata" poster="{rel(a["image"])}" src="{rel(a["video"])}"></video>'
        if a["video"]:
            media += f'<a href="{rel(a["video"])}" target="_blank">Open video full size</a>'
        media += f'<span class="tag">AI-generated {"video" if a["video"] else "image"}</span>'
        if a["voice"]:
            media += f'<audio controls src="{rel(a["voice"])}"></audio><span class="tag">AI-generated voice</span>'
        issues = "".join(issue_html(i) for i in rep["issues"]) or '<p class="small">No issues found.</p>'
        notes = "".join(f'<p class="note">{e(n)}</p>' for n in a["notes"])
        cards.append(f"""<div class="card"><div class="media{" big" if a["video"] else ""}">{media}</div><div class="body">
<h3>Ad {v['id']}: {e(v['angle'])} {badge}</h3>
<div class="meta">{e(v['hook_type'])} &middot; {e(v['persona'])} &middot; {e(v['format'])}</div>{changed_html(v)}
<b>Spoken lines</b><div class="say">{e(v['spoken_lines'])}</div>
<b>Scenes</b><ul>{''.join(f'<li>{e(s)}</li>' for s in v['scenes'])}</ul>
<b>On-screen text</b><ul>{''.join(f'<li>{e(s)}</li>' for s in v['on_screen_text'])}</ul>
<b>Call to action:</b> {e(v['cta'])}
<h2 style="font-size:17px;margin:14px 0 4px">Policy check ({e(', '.join(rep['checked_by']))})</h2>{issues}{notes}
</div></div>""")
    parts = estimate["parts_usd"]
    cost_rows = "".join(f"<tr><td>{e(l['item'])}</td><td>{e(l['detail'])}</td><td>{usd(l['usd'])}</td></tr>" for l in costs.lines) \
        or '<tr><td colspan="3">Nothing was spent in this run (dry run).</td></tr>'
    spent_label = ("spent in total, including test attempts and redone parts"
                   if costs.spent > estimate["total_usd"] * 1.05 else "spent in this run")
    mode_text = {"dry": "Dry run (no paid calls)", "text": "Text only (Claude calls)", "full": "Full run"}[mode]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UGC ad demo: {e(product)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>UGC ad demo: {e(product)}</h1>
<p class="sub">{mode_text} &middot; {datetime.now().strftime('%Y-%m-%d %H:%M')} &middot; {len(variations)} variations from one product brief</p>
<div class="banner"><b>{label}</b> {e(product)} is a made-up example product.</div>{origin_html(origin)}
<div class="costbox"><div><b>{usd(costs.spent)}</b>{spent_label}</div><div><b>{usd(estimate['total_usd'])}</b>estimate for a full run of these ads</div>
<div><b>{usd(estimate['video_per_ad_usd'])}</b>to add video to one ad</div></div>
{next_step_html(mode, run_dir, assets, estimate)}<h2>Overview</h2><div class="scroll"><table><tr><th>#</th><th>Hook type</th><th>Persona</th><th>Format</th><th>Policy check</th>{'<th>What changed</th>' if origin else ''}</tr>{''.join(rows)}</table></div>
<h2>The ads</h2><div class="grid">{''.join(cards)}</div>
<h2>Cost (US dollars)</h2><div class="scroll"><table><tr><th>Item</th><th>Detail</th><th>Cost</th></tr>{cost_rows}</table></div>
<p class="small">Full-run estimate = Claude {usd(parts['claude'])} + pictures {usd(parts['images'])} + voice {usd(parts['voice'])} + video {usd(parts['video'])}.
Prices come from config.toml and change often. Voice cost is rough because ElevenLabs bills through plan credits.</p>
<p class="small">The policy check is a helper that finds obvious problems. A pass is not approval from Meta. Always have a person review ads before running them.</p>
</div></body></html>"""


# ----------------------------------------------------------------------------
# Self-check: a quick test that the setup and the policy rules still work
# ----------------------------------------------------------------------------
def self_check():
    results = []

    def check(name, ok):
        results.append(ok)
        print(("PASS  " if ok else "FAIL  ") + name)

    try:
        cfg = load_config()
        need = [("run", "mode"), ("claude", "script_model"), ("claude", "policy_model"), ("image", "model"),
                ("video", "model"), ("voice", "model_id"), ("labels", "ai_label")]
        check("config.toml has all required settings", all(k2 in cfg.get(k1, {}) for k1, k2 in need))
        check("a price is set for both Claude models",
              all(cfg["claude"][k] in cfg["prices"] for k in ("script_model", "policy_model")))
    except Exception as err:
        check(f"config.toml loads ({err})", False)
        return 1
    product = "SafeNest"
    bad = ["Your phone might already be hacked", "We guarantee 100% protection", "Before and after SafeNest",
           "I am a real customer", "Endorsed by the police"]
    good = ["Paste the message and get a plain answer.", "Try it free for two weeks.", "It looks like a scam because of the link."]
    dummy = lambda t: {"hook": t, "spoken_lines": "", "cta": "", "on_screen_text": [], "scenes": []}
    check("risky phrases are flagged", all(local_flags(dummy(t), product) for t in bad))
    check("normal phrases are not flagged", not any(local_flags(dummy(t), product) for t in good))
    try:
        samples = json.loads((ROOT / "samples" / "sample_variations.json").read_text(encoding="utf-8"))["variations"]
        vs = [clean_variation(r, i) for i, r in enumerate(samples, 1)]
        verdicts = [bool(local_flags(v, product)) for v in vs]
        check("sample scripts load and have all fields", len(vs) >= 3)
        check("risky sample is flagged, clean samples pass", verdicts[-1] and not any(verdicts[:-1]))
    except Exception as err:
        check(f"sample scripts load ({err})", False)
    print("\nAll good." if all(results) else "\nSomething needs attention.")
    return 0 if all(results) else 1


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run_policy_test(cfg, costs, brief):
    """Run the real policy check on the 4 sample scripts (one is deliberately risky). Costs about 1 cent."""
    data = json.loads((ROOT / "samples" / "sample_variations.json").read_text(encoding="utf-8"))
    variations = [clean_variation(r, i) for i, r in enumerate(data["variations"], start=1)]
    print("Policy test: the 4 sample scripts. Ad 4 is deliberately risky and should be REVIEW.\n")
    for r in step_policy(cfg, costs, brief, variations, "text"):
        print(f"Ad {r['id']}: {r['verdict'].upper()}")
        for i in r["issues"]:
            who = "fixed rules" if i["source"] == "rules" else "Claude"
            print(f"    [{i['severity']}] {who}: {i['category']} - \"{i['quote']}\"")
    print(f"\nSpent: {usd(costs.spent)}")


def find_run(cfg, name):
    """The folder of an earlier run: 'latest', a folder name inside runs/, or a path."""
    base = ROOT / cfg["run"]["output_dir"]
    if name == "latest":
        runs = sorted(p for p in base.glob("run_*") if (p / "scripts.json").exists())
        if not runs:
            raise RuntimeError("There is no earlier run to resume.")
        return runs[-1]
    for candidate in (Path(name), base / name):
        if (candidate / "scripts.json").exists():
            return candidate.resolve()
    raise RuntimeError(f"Could not find a finished run called '{name}'.")


def run_voice_test(cfg):
    """Make one short audio file per configured voice, so you can listen and pick. About 1 cent per voice."""
    ids = {}
    for slot, value in cfg["voice"]["ids"].items():
        options = [i for i in as_list(value) if i]
        for n, vid in enumerate(options, start=1):
            ids[slot if len(options) == 1 else f"{slot}_{n}"] = vid
    line = ("Okay, so quick question. Has your mom ever forwarded you a text saying her package is stuck? "
            "Mine did. So now we just paste it in and... yeah, it tells us if it's a scam.")
    folder = ROOT / cfg["run"]["output_dir"] / datetime.now().strftime("voice_test_%Y%m%d_%H%M%S")
    folder.mkdir(parents=True)
    print(f"Voice test: {len(ids)} voice(s), model {cfg['voice']['model_id']}\n")
    for slot, vid in ids.items():
        (folder / f"{slot}.mp3").write_bytes(tts(cfg, vid, line))
        print(f"  made {slot}.mp3")
    cost = len(line) * len(ids) / 1000 * cfg["voice"]["usd_per_1000_characters"]
    print(f"\nListen to the files in: {folder}\nAbout {usd(cost)} (rough).")


def open_file(path):
    try:
        if "microsoft" in platform.release().lower():      # Windows with WSL
            win = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()
            subprocess.Popen(["explorer.exe", win])
        else:
            import webbrowser
            webbrowser.open(path.as_uri())
    except Exception:
        print("Could not open it automatically. Open the file by hand.")


def main():
    ap = argparse.ArgumentParser(description="Make UGC-style ad variations from a product brief.")
    ap.add_argument("--mode", choices=["dry", "text", "full"], help="overrides the mode in config.toml")
    ap.add_argument("--brief", help="path to a product brief (default: from config.toml)")
    ap.add_argument("-n", type=int, help="number of variations (3 to 5)")
    ap.add_argument("--video-for", type=int, nargs="*", default=[], metavar="ID", help="make a real video only for these ad numbers")
    ap.add_argument("--showcase", action="store_true", help="use the better (more expensive) video model")
    ap.add_argument("--open", action="store_true", help="open the summary page when done")
    ap.add_argument("--check", action="store_true", help="run the quick self-check and stop")
    ap.add_argument("--policy-test", action="store_true", help="run the Claude policy check on the sample scripts (about 1 cent)")
    ap.add_argument("--voice-test", action="store_true", help="make one short audio sample per configured voice, to listen and choose")
    ap.add_argument("--resume", nargs="?", const="latest", metavar="RUN",
                    help="finish an earlier run: keep the files already made and make only what is missing "
                         "(RUN is a folder name, or 'latest')")
    ap.add_argument("--redo", nargs="+", choices=["image", "voice", "video"],
                    help="with --resume: make these pieces again for every ad")
    ap.add_argument("--variations-of", type=int, metavar="ID",
                    help="make new versions of this ad from an earlier run (use with --change)")
    ap.add_argument("--change", help="the ONE thing to change in those versions, in plain words")
    ap.add_argument("--keep-person", action="store_true",
                    help="with --variations-of: always keep the original person, picture and voice, whatever Claude writes")
    ap.add_argument("--from-run", default="latest", metavar="RUN", help="with --variations-of: which run (default: latest)")
    args = ap.parse_args()

    if args.check:
        sys.exit(self_check())

    load_env()
    cfg = load_config(showcase=args.showcase)
    if args.voice_test:
        run_voice_test(cfg)
        return
    resuming = args.resume is not None
    varying = args.variations_of is not None
    if varying and resuming:
        sys.exit("Stopped: --variations-of starts a new run. Do not combine it with --resume.")
    if varying and not (args.change or "").strip():
        sys.exit('Stopped: say the one thing to change, for example --change "a different opening line".')
    if args.keep_person and not varying:
        sys.exit("Stopped: --keep-person only works together with --variations-of.")
    if args.change and not varying:
        sys.exit("Stopped: --change only works together with --variations-of.")
    if args.redo and not resuming:
        sys.exit("Stopped: --redo only works together with --resume.")
    if resuming and args.mode not in (None, "full"):
        sys.exit("Stopped: --resume only works in full mode.")
    mode = "full" if resuming else (args.mode or cfg["run"]["mode"])
    n = max(1, min(5, args.n or 3)) if varying else max(3, min(5, args.n or cfg["run"]["variations"]))
    run_dir = find_run(cfg, args.resume) if resuming else None
    src_run = find_run(cfg, args.from_run) if varying else None
    brief = (run_dir / "brief.md" if resuming else src_run / "brief.md" if varying
             else Path(args.brief or ROOT / cfg["run"]["brief_file"])).read_text(encoding="utf-8")
    product = product_name(brief)
    video_ids = set(args.video_for)
    costs = Costs(cfg)

    if args.policy_test:
        run_policy_test(cfg, costs, brief)
        return

    origin, voice_ids = None, {}
    if resuming and (run_dir / "variation_of.json").exists():
        origin = json.loads((run_dir / "variation_of.json").read_text(encoding="utf-8"))
    if resuming:
        costs.load_previous(run_dir / "costs.json")
        print(f"Mode: {mode} (resuming)   Product: {product}   Output: {run_dir}")
        print("Steps 1-2  Keeping the scripts and the policy check from this run ...")
        variations = [clean_variation(r, i) for i, r in enumerate(
            json.loads((run_dir / "scripts.json").read_text(encoding="utf-8")), start=1)]
        report = json.loads((run_dir / "policy_report.json").read_text(encoding="utf-8"))
    else:
        suffix = f"_ad{args.variations_of}_variations" if varying else ""
        run_dir = ROOT / cfg["run"]["output_dir"] / (datetime.now().strftime("run_%Y%m%d_%H%M%S") + suffix)
        run_dir.mkdir(parents=True)
        (run_dir / "brief.md").write_text(brief, encoding="utf-8")
        print(f"Mode: {mode}   Product: {product}   Output: {run_dir}")

        costs.debug_dir = run_dir
        if varying:
            src_vars = [clean_variation(r, i) for i, r in enumerate(
                json.loads((src_run / "scripts.json").read_text(encoding="utf-8")), start=1)]
            base = next((b for b in src_vars if b["id"] == args.variations_of), None)
            if base is None:
                raise RuntimeError(f"Ad {args.variations_of} is not in {src_run.name} (it has ads 1 to {len(src_vars)}).")
            print(f"Step 1/4  Writing {n} version(s) of ad {base['id']}. Only this changes: {args.change.strip()}")
            variations = step_variations(cfg, costs, brief, mode, base, args.change.strip(), n)
            if args.keep_person:
                for v in variations:
                    for k in AVATAR_FIELDS:
                        v[k] = base[k]
                print("          --keep-person: every version keeps the original person, picture and voice.")
            origin = {"from_run": src_run.name, "base_ad": base["id"], "change": args.change.strip()}
            (run_dir / "variation_of.json").write_text(json.dumps(origin, indent=2, ensure_ascii=False), encoding="utf-8")
            src_image = existing_image(src_run / f"ad_{base['id']}")
            src_voice = base_voice_id(cfg, src_run, src_vars, base["id"])
            for v in variations:
                if all(v.get(k) == base.get(k) for k in AVATAR_FIELDS):      # same person: keep the picture and the voice
                    voice_ids[v["id"]] = src_voice
                    if src_image:
                        (run_dir / f"ad_{v['id']}").mkdir(exist_ok=True)
                        shutil.copy(src_image, run_dir / f"ad_{v['id']}" / src_image.name)
                        print(f"          Ad {v['id']}: same person, so the original picture is reused.")
        else:
            print("Step 1/4  Writing angles, hooks and scripts ...")
            variations = step_scripts(cfg, costs, brief, mode, n)
        (run_dir / "scripts.json").write_text(json.dumps(variations, indent=2, ensure_ascii=False), encoding="utf-8")

        print("Step 2/4  Checking claims against Facebook policy ...")
        report = step_policy(cfg, costs, brief, variations, mode)
        (run_dir / "policy_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        for r in report:
            tips = sum(1 for i in r["issues"] if i.get("severity") == "tip")
            print(f"          Ad {r['id']}: {r['verdict'].upper()}  ({len(r['issues']) - tips} fix(es), {tips} tip(s))")

    spent_before = costs.spent
    print("Step 3/4  Making pictures, voice and video" + (" (only what is missing) ..." if resuming else " ..."))
    prompts, assets, voice_use = {}, {}, {}
    for v in variations:
        prompts[v["id"]] = build_prompts(v, cfg)
        if voice_ids.get(v["id"]):
            prompts[v["id"]]["voice_id"] = voice_ids[v["id"]]
        assets[v["id"]] = make_assets(cfg, costs, v, prompts[v["id"]], run_dir / f"ad_{v['id']}", mode,
                                      wants_video(cfg, v, video_ids), voice_use, set(args.redo or []))
        for note in assets[v["id"]]["notes"]:
            print(f"          Ad {v['id']}: {note}")
    (run_dir / "prompts.json").write_text(json.dumps(prompts, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Step 4/4  Writing the summary page ...")
    with_video = video_ids | {i for i, a in assets.items() if a["video"]}      # videos already made count in the estimate
    estimate = estimate_full(cfg, variations, costs, mode, with_video)
    (run_dir / "costs.json").write_text(json.dumps(
        {"spent_usd": costs.spent, "lines": costs.lines, "full_run_estimate": estimate}, indent=2), encoding="utf-8")
    summary = run_dir / "summary.html"
    summary.write_text(build_summary(cfg, product, mode, run_dir, variations, report, assets, prompts, costs, estimate, origin), encoding="utf-8")

    if resuming:
        print(f"\nDone. Spent just now: {usd(costs.spent - spent_before)}.  Total for this run: {usd(costs.spent)}.")
    else:
        print(f"\nDone. Spent in this run: {usd(costs.spent)}.  Estimate for a full run: {usd(estimate['total_usd'])}.")
    print(f"Summary page: {summary}")
    if args.open:
        open_file(summary)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as err:
        sys.exit(f"Stopped: {err}")
