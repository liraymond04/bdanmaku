#!/usr/bin/env python3
"""
Danmaku translation helper for bdanmaku.lua.
Reads a biliass-generated ASS file, translates Chinese danmaku via
Google Translate (no API key), and writes a new ASS file with
translations displayed below each original danmaku.
Supports --cache to persist translations across re-runs (e.g. on resize).
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ASS_ESCAPE = str.maketrans({'{': '\\{', '}': '\\}'})


def translate_text(text: str, target: str = "en") -> str | None:
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": target,
        "dt": "t",
        "q": text,
    }
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            parts = [s[0] for s in data[0] if s[0] is not None]
            return "".join(parts)
    except Exception as e:
        print(f"  [translate error] {text[:40]}: {e}", file=sys.stderr)
        return None


def has_cjk(text: str) -> bool:
    return bool(re.search(
        r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
        r"\u3040-\u309f\u30a0-\u30ff"
        r"\uac00-\ud7af\u1100-\u11ff]",
        text
    ))


def parse_ass_style_fontsize(content: str, style_name: str = "biliass") -> int:
    for line in content.split("\n"):
        if line.startswith(f"Style: {style_name},"):
            parts = line.split(",")
            if len(parts) >= 3:
                try:
                    return int(parts[2].strip())
                except ValueError:
                    pass
    return 48


def load_translation_cache(cache_path: str) -> dict[str, str]:
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_translation_cache(cache_path: str, cache: dict[str, str]) -> None:
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def batch_translate(
    texts: list[str],
    target: str,
    workers: int = 4,
    existing_cache: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str | None], int]:
    existing = existing_cache or {}
    unique = [t for t in dict.fromkeys(texts) if t not in existing]

    if not unique:
        print(f"  All {len(texts)} texts already cached, skipping API calls", file=sys.stderr)
        return existing, {}, 0

    print(f"  {len(unique)} new texts to translate, {len(existing)} from cache", file=sys.stderr)

    results: dict[str, str | None] = {}
    total = len(unique)
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_text = {pool.submit(translate_text, t, target): t for t in unique}
        for future in as_completed(future_to_text):
            t = future_to_text[future]
            try:
                result = future.result()
            except Exception as e:
                print(f"  [exec error] {t[:40]}: {e}", file=sys.stderr)
                result = None
            results[t] = result
            done += 1
            status = result[:40] if result else "FAILED"
            print(f"  [{done}/{total}] {t[:40]} → {status}", file=sys.stderr)

    new_ok: dict[str, str] = {t: r for t, r in results.items() if r is not None}
    merged = {**existing, **new_ok}
    return merged, results, total


def process_ass(
    input_path: str,
    output_path: str,
    target_lang: str = "en",
    fontsize_ratio: float = 0.7,
    color: str = "&H00FFFF80",
    workers: int = 4,
    cache_path: str | None = None,
    mode: str = "offset",
) -> None:
    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    fontsize = parse_ass_style_fontsize(content)
    y_offset = 0
    orig_new_fs = 0
    if mode == "inline":
        # Split original fontsize between original and translation
        # so combined height equals the original fontsize (no overlaps).
        orig_new_fs = max(14, int(fontsize / (1.0 + fontsize_ratio)))
        trans_fs = max(10, int(orig_new_fs * fontsize_ratio))
    else:
        trans_fs = max(18, int(fontsize * fontsize_ratio))
        y_offset = fontsize + 4

    dialogue_re = re.compile(
        r"^Dialogue:\s*(\d+),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),(.*)$"
    )
    move_re = re.compile(r"\\move\(([^)]+)\)")
    pos_re = re.compile(r"\\pos\(([^)]+)\)")

    lines = content.split("\n")

    class DialogueInfo:
        __slots__ = ("line_idx", "layer", "start", "end", "style", "marginL",
                     "marginR", "marginV", "text", "display_text", "move_params",
                     "pos_params", "an_tag")

        def __init__(self) -> None:
            self.move_params: str | None = None
            self.pos_params: str | None = None
            self.an_tag = ""

    dialogue_infos: list[DialogueInfo] = []
    texts_to_translate: list[str] = []
    in_events = False

    for i, line in enumerate(lines):
        if line.strip() == "[Events]":
            in_events = True
            continue
        if not in_events:
            continue
        if line.startswith("Format:") or line.startswith(";"):
            continue
        m = dialogue_re.match(line)
        if not m:
            continue

        layer, start, end, style, name, marginL, marginR, marginV, effect, text = m.groups()
        text_parts = re.split(r"\{[^}]*\}", text)
        display_text = text_parts[-1].strip() if text_parts else ""
        if not display_text or not has_cjk(display_text):
            continue

        info = DialogueInfo()
        info.line_idx = i
        info.layer = layer
        info.start = start
        info.end = end
        info.style = style
        info.marginL = marginL
        info.marginR = marginR
        info.marginV = marginV
        info.text = text
        info.display_text = display_text

        move_match = move_re.search(text)
        if move_match:
            info.move_params = move_match.group(1)
        else:
            pos_match = pos_re.search(text)
            if pos_match:
                info.pos_params = pos_match.group(1)
                an_match = re.search(r"\\an(\d+)", text)
                info.an_tag = f"\\an{an_match.group(1)}" if an_match else ""

        # In offset mode, skip danmaku without positioning tags
        # (we need coords to place the translation).
        # In inline mode, every CJK danmaku is eligible.
        if mode == "offset" and info.move_params is None and info.pos_params is None:
            continue

        dialogue_infos.append(info)
        texts_to_translate.append(display_text)

    if not dialogue_infos:
        print("No CJK danmaku found, copying file as-is", file=sys.stderr)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return

    existing_cache = load_translation_cache(cache_path) if cache_path else {}
    merged_cache, _results, new_count = batch_translate(
        texts_to_translate, target_lang, workers, existing_cache
    )

    if cache_path and new_count > 0:
        save_translation_cache(cache_path, merged_cache)

    new_lines = list(lines)
    translated_count = 0

    if mode == "inline":
        for info in dialogue_infos:
            translated = merged_cache.get(info.display_text)
            if not translated:
                continue

            esc_translated = translated.translate(_ASS_ESCAPE)

            orig_line = new_lines[info.line_idx]
            m = dialogue_re.match(orig_line)
            if not m:
                continue
            groups = list(m.groups())
            orig_text = groups[9]

            idx = orig_text.rfind(info.display_text)
            if idx == -1:
                new_display = f"{{\\fs{orig_new_fs}}}{orig_text}\\N{{\\fs{trans_fs}\\c{color}\\alpha&H00}}{esc_translated}"
            else:
                new_display = (
                    orig_text[:idx]
                    + f"{{\\fs{orig_new_fs}}}"
                    + orig_text[idx:]
                    + f"\\N{{\\fs{trans_fs}\\c{color}\\alpha&H00}}{esc_translated}"
                )
            groups[9] = new_display
            new_lines[info.line_idx] = "Dialogue: " + ",".join(groups)
            translated_count += 1
    else:
        offset = 0

        for info in dialogue_infos:
            translated = merged_cache.get(info.display_text)
            if not translated:
                continue

            esc_translated = translated.translate(_ASS_ESCAPE)
            new_layer = str(int(info.layer) + 2) if info.layer.isdigit() else info.layer

            override: str | None = None
            if info.move_params:
                parts = [p.strip() for p in info.move_params.split(",")]
                if len(parts) >= 4:
                    x1, y1, x2, y2 = parts[0], parts[1], parts[2], parts[3]
                    ny1 = str(int(y1) + y_offset)
                    ny2 = str(int(y2) + y_offset)
                    override = (
                        f"{{\\move({x1}, {ny1}, {x2}, {ny2})"
                        f"\\fs{trans_fs}\\c{color}\\alpha&H00}}"
                    )
            elif info.pos_params:
                parts = [p.strip() for p in info.pos_params.split(",")]
                if len(parts) >= 2:
                    x, y = parts[0], parts[1]
                    ny = str(int(y) + y_offset)
                    override = (
                        f"{{{info.an_tag}\\pos({x}, {ny})"
                        f"\\fs{trans_fs}\\c{color}\\alpha&H00}}"
                    )

            if override is None:
                continue

            trans_line = (
                f"Dialogue: {new_layer},{info.start},{info.end},{info.style},,"
                f"{info.marginL},{info.marginR},{info.marginV},,{override}{esc_translated}"
            )
            insert_idx = info.line_idx + 1 + offset
            new_lines.insert(insert_idx, trans_line)
            offset += 1
            translated_count += 1

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))

    cache_msg = f" ({new_count} new API calls)" if new_count > 0 else " (all cached)"
    print(f"Translated {translated_count}/{len(dialogue_infos)} danmaku lines{cache_msg}", file=sys.stderr)


def main() -> None:
    parser = ArgumentParser(
        description="Add translations below danmaku in a biliass-generated ASS file"
    )
    parser.add_argument("input", help="Input ASS file path")
    parser.add_argument("output", help="Output ASS file path")
    parser.add_argument("--target", default="en", help="Target language code (default: en)")
    parser.add_argument("--ratio", type=float, default=0.7, help="Font size ratio (default: 0.7)")
    parser.add_argument("--color", default="&H00FFFF80", help="ASS hex color (default: &H00FFFF80)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers (default: 4)")
    parser.add_argument("--cache", default=None, help="Path to JSON translation cache file")
    parser.add_argument("--mode", default="offset", choices=["offset", "inline"],
                        help="Layout mode: 'offset' places translation below (separate line), "
                             "'inline' embeds it in the original line (no overlap, "
                             "scaled fonts to keep original height). Default: offset")
    args = parser.parse_args()
    process_ass(args.input, args.output, args.target, args.ratio, args.color,
                args.workers, args.cache, mode=args.mode)


if __name__ == "__main__":
    main()
