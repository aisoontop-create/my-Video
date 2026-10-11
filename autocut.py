#!/usr/bin/env python3
"""autocut.py — ตัดช่วงเงียบอัตโนมัติ + jump-cut zoom + color grade + ซับไทย + progress bar

ใช้งาน:  python autocut.py input.mp4 [--silence-db -35] [--min-silence 0.4] [--no-subs] ...
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request

# ===== ตั้งค่า (แก้ตรงนี้ หรือส่งผ่าน CLI) =====
INPUT = "input.mp4"
SILENCE_DB = -35        # เสียงเบากว่านี้ (dB) ถือว่าเงียบ
MIN_SILENCE = 0.4       # เงียบนานกว่ากี่วินาทีถึงจะตัด
PADDING = 0.12          # เว้นขอบก่อน/หลังเสียงพูด กันคำขาด
ZOOM = 1.12             # ระดับซูมของ jump-cut
LANGUAGE = "th"         # ภาษาซับ
SUBTITLES = True
PROGRESS_BAR = True

MERGE_GAP = 0.15        # รวมช่วงที่ห่างกันน้อยกว่านี้
MIN_KEEP = 0.25         # ทิ้งช่วงที่สั้นกว่านี้
AFADE = 0.01            # audio fade ที่รอยตัด (10ms)
FADE_IN, FADE_OUT = 0.3, 0.5
BAR_H = 6
BAR_COLOR = "0xFFD400"

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
FONT_NAME = "Noto Sans Thai"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansthai/NotoSansThai%5Bwdth%2Cwght%5D.ttf"


def log(msg):
    print(msg, flush=True)


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit(f"คำสั่งล้มเหลว: {' '.join(cmd[:6])} ...")
    return r


# ---------- 0. เช็กเครื่อง ----------
def check_tools():
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    hint = {
        "Darwin": "brew install ffmpeg",
        "Windows": "winget install Gyan.FFmpeg  (หรือ choco install ffmpeg)",
    }.get(platform.system(), "sudo apt install ffmpeg  (Fedora: sudo dnf install ffmpeg)")
    raise SystemExit(f"ไม่พบ ffmpeg/ffprobe — ติดตั้งด้วย: {hint}")


def pip_install(*pkgs):
    log(f"ติดตั้ง {' '.join(pkgs)} ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pkgs], check=True)


def ensure_tqdm():
    try:
        from tqdm import tqdm  # noqa: F401
    except ImportError:
        pip_install("tqdm")


def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise SystemExit("ไฟล์นี้ไม่มี video stream")
    w, h = int(v["width"]), int(v["height"])
    # รองรับคลิปมือถือที่หมุนภาพไว้ (rotation metadata)
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot) or 0)
    rot = int(v.get("tags", {}).get("rotate", rot))
    if abs(rot) % 180 == 90:
        w, h = h, w
    num, den = (v.get("avg_frame_rate") or v["r_frame_rate"]).split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return {"w": w, "h": h, "fps": fps, "fps_str": f"{num}/{den}",
            "dur": float(info["format"]["duration"]), "has_audio": a is not None}


# ---------- 1. หาช่วงเงียบ ----------
def detect_silence(path, db, min_sil):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
                        "-af", f"silencedetect=noise={db}dB:d={min_sil}", "-f", "null", "-"],
                       stderr=subprocess.PIPE, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends + [None] * (len(starts) - len(ends))))


def keep_segments(silences, dur, pad):
    keeps, cur = [], 0.0
    for s, e in silences:
        e = dur if e is None else e
        keeps.append((cur, max(0.0, s)))
        cur = e
    keeps.append((cur, dur))
    keeps = [(max(0.0, a - pad), min(dur, b + pad)) for a, b in keeps if b > a]
    merged = []
    for a, b in keeps:
        if merged and a - merged[-1][1] < MERGE_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= MIN_KEEP]


# ---------- 2. render แต่ละช่วง ----------
def even(x):
    return int(round(x / 2)) * 2


def render_segments(src, info, segs, zoom, work):
    from tqdm import tqdm
    w, h = info["w"], info["h"]
    zw, zh = even(w * zoom), even(h * zoom)
    grade = "eq=contrast=1.08:saturation=1.15,vignette=angle=PI/5"
    files = []
    for i, (a, b) in enumerate(tqdm(segs, desc="ตัดช่วง", unit="seg")):
        d = b - a
        vf = [f"scale={w}:{h}:flags=lanczos,setsar=1"]
        if i % 2 == 1:   # ช่วงคี่ = punch-in
            vf.append(f"scale={zw}:{zh}:flags=lanczos,crop={w}:{h}:(iw-{w})/2:(ih-{h})/2,setsar=1")
        vf += [grade, f"fps={info['fps_str']}", "format=yuv420p"]
        out = os.path.join(work, f"seg_{i:05d}.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-i", src, "-t", f"{d:.3f}",
               "-vf", ",".join(vf)]
        if info["has_audio"]:
            cmd += ["-af", f"afade=t=in:d={AFADE},afade=t=out:st={max(0, d - AFADE):.3f}:d={AFADE},"
                           "aresample=48000", "-ac", "2", "-c:a", "aac", "-b:a", "192k"]
        else:  # ใส่เสียงเงียบไว้ให้ concat ได้สม่ำเสมอ
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                    "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
                "-video_track_timescale", "90000", out]
        run(cmd)
        files.append(out)
    return files


def concat(files, work):
    lst = os.path.join(work, "list.txt")
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{os.path.basename(p)}'\n")
    out = os.path.join(work, "cut.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
         "-c", "copy", "cut.mp4"], cwd=work)
    return out


# ---------- 4. ซับไตเติล ----------
def ensure_font():
    r = subprocess.run(["fc-list", ":lang=th", "family"], stdout=subprocess.PIPE, text=True) \
        if shutil.which("fc-list") else None
    if r and FONT_NAME in r.stdout:
        return
    os.makedirs(FONT_DIR, exist_ok=True)
    dst = os.path.join(FONT_DIR, "NotoSansThai.ttf")
    if not os.path.exists(dst):
        log("ดาวน์โหลดฟอนต์ Noto Sans Thai ...")
        try:
            req = urllib.request.Request(FONT_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
                f.write(r.read())
        except Exception as e:
            log(f"ดาวน์โหลดฟอนต์ไม่สำเร็จ ({e}) — วางไฟล์ .ttf ภาษาไทยไว้ใน {FONT_DIR} เอง")


def transcribe(path, lang):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        pip_install("faster-whisper")
        from faster_whisper import WhisperModel
    try:
        import ctranslate2
        gpu = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        gpu = False
    size = "medium" if gpu else "small"
    log(f"ถอดเสียงด้วย faster-whisper ({size}, {'GPU' if gpu else 'CPU'}) ...")
    model = WhisperModel(size, device="cuda" if gpu else "cpu",
                         compute_type="float16" if gpu else "int8")
    segs, _ = model.transcribe(path, language=lang, word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for wd in s.words or []:
            if wd.word.strip():
                words.append((wd.start, wd.end, wd.word))
    return words


def chunk_words(words, lang):
    chunks, cur = [], []
    for wd in words:
        text_len = sum(len(x[2].strip()) for x in cur)
        if cur and (len(cur) >= 4 or wd[0] - cur[-1][1] > 0.6
                    or (len(cur) >= 2 and (text_len > 18 or cur[-1][2].strip()[-1:] in ".?!,"))):
            chunks.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        chunks.append(cur)
    return chunks


def ts_ass(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ts_srt(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def join(ws, lang):
    sep = "" if lang in ("th", "zh", "ja", "lo", "km", "my") else " "
    return sep.join(w.strip() for w in ws).strip()


def write_subs(chunks, info, ass_path, srt_path, lang):
    w, h = info["w"], info["h"]
    size = int(min(w, h) * (0.075 if h > w else 0.065))
    margin_v = int(h * (0.18 if h > w else 0.08))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{FONT_NAME},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{max(3, size // 12)},{max(2, size // 25)},2,{int(w * .06)},{int(w * .06)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, srt = [], []
    sep = "" if lang in ("th", "zh", "ja", "lo", "km", "my") else " "
    for n, ch in enumerate(chunks):
        texts = [x[2].strip() for x in ch]
        chunk_end = ch[-1][1] + 0.15
        if n + 1 < len(chunks):
            chunk_end = min(chunk_end, chunks[n + 1][0][0])
        for i, (st, _en, _) in enumerate(ch):
            en = ch[i + 1][0] if i + 1 < len(ch) else chunk_end
            if en <= st:
                continue
            parts = [("{\\c&H00D4FF&}" + t + "{\\c&HFFFFFF&}") if j == i else t
                     for j, t in enumerate(texts)]
            pop = "{\\fscx108\\fscy108\\t(0,80,\\fscx100\\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{ts_ass(st)},{ts_ass(en)},Main,,0,0,0,,{pop}{sep.join(parts)}")
        srt.append(f"{n + 1}\n{ts_srt(ch[0][0])} --> {ts_srt(chunk_end)}\n{join(texts, lang)}\n")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt))


# ---------- 3+5. เสียง + finishing + export ----------
def finish(cut, info, dur, out, work, ass_name, progress):
    w, fade_out_st = info["w"], max(0, dur - FADE_OUT)
    vchain = f"[0:v]fade=t=in:d={FADE_IN},fade=t=out:st={fade_out_st:.3f}:d={FADE_OUT}"
    if ass_name:
        fd = os.path.relpath(FONT_DIR, work) if os.path.isdir(FONT_DIR) else "."
        vchain += f",subtitles={ass_name}:fontsdir={fd}"
    if progress:
        fc = (f"{vchain}[base];color=c={BAR_COLOR}:s={w}x{BAR_H}:r={info['fps_str']}[bar];"
              f"[base][bar]overlay=x='-{w}+{w}*t/{dur:.3f}':y=0:shortest=1,format=yuv420p[v]")
    else:
        fc = f"{vchain},format=yuv420p[v]"
    af = (f"highpass=f=80,afftdn=nf=-25,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
          f"afade=t=in:d={FADE_IN},afade=t=out:st={fade_out_st:.3f}:d={FADE_OUT}")
    fc += f";[0:a]{af}[a]"
    run(["ffmpeg", "-y", "-v", "error", "-stats", "-i", os.path.abspath(cut),
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-r", info["fps_str"],
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", os.path.abspath(out)], cwd=work)


def main():
    p = argparse.ArgumentParser(description="ตัดช่วงเงียบ + เอฟเฟ็กต์ + ซับอัตโนมัติ")
    p.add_argument("input", nargs="?", default=INPUT)
    p.add_argument("--silence-db", type=float, default=SILENCE_DB)
    p.add_argument("--min-silence", type=float, default=MIN_SILENCE)
    p.add_argument("--padding", type=float, default=PADDING)
    p.add_argument("--zoom", type=float, default=ZOOM)
    p.add_argument("--lang", default=LANGUAGE)
    p.add_argument("--no-subs", action="store_true", default=not SUBTITLES)
    p.add_argument("--no-progress", action="store_true", default=not PROGRESS_BAR)
    p.add_argument("--keep-temp", action="store_true")
    p.add_argument("-o", "--output")
    args = p.parse_args()

    check_tools()
    ensure_tqdm()
    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        raise SystemExit(f"ไม่พบไฟล์ {src}")
    base, _ = os.path.splitext(src)
    out = os.path.abspath(args.output or f"{base}_edited.mp4")
    if out == src:
        raise SystemExit("ชื่อไฟล์ output ซ้ำกับต้นฉบับ — ไม่เขียนทับ")
    work = os.path.join(os.path.dirname(out), "_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    info = probe(src)
    log(f"ต้นฉบับ: {info['w']}x{info['h']} @ {info['fps']:.2f}fps, {info['dur']:.2f}s, "
        f"{'แนวตั้ง' if info['h'] > info['w'] else 'แนวนอน'}")

    if info["has_audio"]:
        sil = detect_silence(src, args.silence_db, args.min_silence)
        segs = keep_segments(sil, info["dur"], args.padding)
    else:
        log("ไม่มีเสียง — ข้ามการตัดช่วงเงียบ")
        sil, segs = [], [(0.0, info["dur"])]
    if not segs:
        raise SystemExit("ไม่เหลือช่วงที่มีเสียงเลย ลองลด --silence-db (เช่น -45)")
    log(f"เจอช่วงเงียบ {len(sil)} ช่วง → เก็บไว้ {len(segs)} ช่วง")

    files = render_segments(src, info, segs, args.zoom, work)
    cut = concat(files, work)
    new_dur = probe(cut)["dur"]

    ass_name = None
    if not args.no_subs and info["has_audio"]:
        ensure_font()
        words = transcribe(cut, args.lang)
        if words:
            ass_name = "subs.ass"
            srt_path = f"{os.path.splitext(out)[0]}.srt"
            write_subs(chunk_words(words, args.lang), info, os.path.join(work, ass_name),
                       srt_path, args.lang)
            log(f"บันทึกซับ: {srt_path}")
        else:
            log("ไม่พบคำพูดในคลิป — ข้ามซับ")

    log("Export ไฟล์สุดท้าย ...")
    finish(cut, info, new_dur, out, work, ass_name, not args.no_progress)

    o = probe(out)
    if not o["has_audio"]:
        raise SystemExit("ผิดพลาด: output ไม่มี audio stream")
    cut_pct = 100 * (1 - o["dur"] / info["dur"])
    log("\n===== สรุป =====")
    log(f"ความยาว: {info['dur']:.2f}s → {o['dur']:.2f}s  (ลดลง {cut_pct:.1f}%)")
    log(f"ตัดช่วงเงียบออก {len(sil)} ช่วง, เหลือ {len(segs)} ช่วง")
    log(f"Output: {out}  ({o['w']}x{o['h']} @ {o['fps']:.2f}fps, มีทั้งภาพและเสียง)")

    if args.keep_temp:
        log(f"เก็บไฟล์ชั่วคราวไว้ที่ {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
