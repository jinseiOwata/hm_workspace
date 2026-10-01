"""台本(scripts.json)から YouTube ショート動画(1080x1920, mp4)を作る。

使い方:
    python3 shorts/make_short.py shorts/2026-10-05/scripts.json            # 全部
    python3 shorts/make_short.py shorts/2026-10-05/scripts.json 01_ichiendama  # 1本だけ

必要なもの(Ubuntu): ffmpeg, open-jtalk, open-jtalk-mecab-naist-jdic,
hts-voice-nitech-jp-atr503-m001, fonts-noto-cjk, fonts-noto-color-emoji, pip の pillow
"""
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FPS = 30
PAD = 0.35  # 各セリフの後の間(秒)
SPEED = 1.15  # 読み上げ速度
CHANNEL = "雑学アーカイブ"

FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
JTALK_DIC = "/var/lib/mecab/dic/open-jtalk/naist-jdic"
JTALK_VOICE = "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice"


def font(size):
    return ImageFont.truetype(FONT_BOLD, size, index=0)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def tts(text, out):
    subprocess.run(
        ["open_jtalk", "-x", JTALK_DIC, "-m", JTALK_VOICE, "-r", str(SPEED), "-ow", str(out)],
        input=text.encode("utf-8"), check=True)
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def emoji_image(ch, size):
    f = ImageFont.truetype(FONT_EMOJI, 109)  # NotoColorEmoji は 109px 固定
    im = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((10, 10), ch, font=f, embedded_color=True)
    im = im.crop(im.getbbox())
    r = size / max(im.size)
    return im.resize((round(im.width * r), round(im.height * r)), Image.LANCZOS)


def fit_lines(draw, lines, size, max_w=960):
    """一番長い行が max_w に収まるまでフォントを小さくする。"""
    while size > 40:
        f = font(size)
        if all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f
        size -= 4
    return font(size)


def background(bg):
    top, bottom = hex_rgb(bg[0]), hex_rgb(bg[1])
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    # うっすらドット模様
    dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for y in range(0, H, 60):
        for x in range((y // 60) % 2 * 30, W, 60):
            dd.ellipse([x, y, x + 4, y + 4], fill=(255, 255, 255, 14))
    im.paste(dots, (0, 0), dots)
    return im


def render(video, line, idx, total, out):
    accent = hex_rgb(video["accent"])
    kind = line.get("kind", "body")
    im = background(video["bg"])
    d = ImageDraw.Draw(im)

    # 進行バー(上部。YouTube の UI に隠れない位置)
    d.rounded_rectangle([90, 150, W - 90, 164], 7, fill=(80, 86, 104))
    d.rounded_rectangle([90, 150, 90 + (W - 180) * (idx + 1) / total, 164], 7, fill=accent)

    # チャンネル名バッジ
    f = font(38)
    tw = d.textlength(CHANNEL, font=f)
    d.rounded_rectangle([(W - tw) / 2 - 30, 200, (W + tw) / 2 + 30, 266], 33, fill=accent)
    d.text((W / 2, 233), CHANNEL, font=f, fill=(20, 20, 20), anchor="mm")

    # タイトル(フック以外で常に表示)
    if kind != "hook":
        tl = [video["title"]]
        ft = fit_lines(d, tl, 54, 940)
        d.text((W / 2, 340), tl[0], font=ft, fill=accent, anchor="mm",
               stroke_width=3, stroke_fill=(0, 0, 0))

    # 絵文字
    esize = 300 if kind in ("hook", "answer") else 250
    if line.get("emoji"):
        em = emoji_image(line["emoji"], esize)
        im.paste(em, (round((W - em.width) / 2), 520 - em.height // 2 + 80), em)

    # 字幕
    lines = line["sub"].split("\n")
    base = {"hook": 104, "answer": 96, "cta": 92}.get(kind, 92)
    fs = fit_lines(d, lines, base)
    lh = round(fs.size * 1.32)
    y0 = 1080 - (lh * len(lines)) / 2 + 60
    color = accent if kind in ("hook", "answer") else (255, 255, 255)
    if kind in ("answer", "cta"):
        pad = 40
        bw = max(d.textlength(l, font=fs) for l in lines)
        box = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(box).rounded_rectangle(
            [(W - bw) / 2 - pad, y0 - pad, (W + bw) / 2 + pad, y0 + lh * len(lines) + pad - 20],
            36, fill=(0, 0, 0, 120), outline=accent + (255,), width=6)
        im.paste(box, (0, 0), box)
    for i, l in enumerate(lines):
        d.text((W / 2, y0 + lh * i + lh / 2), l, font=fs, fill=color, anchor="mm",
               stroke_width=8, stroke_fill=(0, 0, 0))

    im.save(out)


def make(video, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        segs = []
        for i, line in enumerate(video["lines"]):
            wav, png, seg = tmp / f"{i}.wav", tmp / f"{i}.png", tmp / f"{i}.mp4"
            dur = tts(line["say"], wav) + PAD
            render(video, line, i, len(video["lines"]), png)
            n = round(dur * FPS)
            d_s = n / FPS
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-loop", "1", "-framerate", str(FPS), "-t", f"{d_s:.3f}", "-i", str(png), "-i", str(wav),
                "-filter_complex",
                # ゆっくりズームイン(1.00→1.04倍)
                f"[0:v]scale=w='trunc({W}*(1+0.04*t/{d_s:.3f})/2)*2':h=-2:eval=frame,"
                f"crop={W}:{H},setsar=1,format=yuv420p[v];"
                f"[1:a]aresample=48000,apad,atrim=0:{d_s:.3f}[a]",
                "-map", "[v]", "-map", "[a]", "-frames:v", str(n),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-ac", "2", str(seg)], check=True)
            segs.append(seg)
            if i == 0:
                png_keep = out_dir / f"{video['id']}_cover.png"
                Image.open(png).save(png_keep)
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in segs))
        out = out_dir / f"{video['id']}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)], check=True)
    return out


def main():
    src = Path(sys.argv[1])
    only = set(sys.argv[2:])
    for video in json.loads(src.read_text()):
        if only and video["id"] not in only:
            continue
        out = make(video, src.parent / "videos")
        print("done:", out)


if __name__ == "__main__":
    main()
