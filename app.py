"""Shorts Maker: turn a long video into ~1 minute vertical clips with burned-in English captions."""
import shutil
import subprocess
import tempfile
from pathlib import Path

import streamlit as st

MAX_MINUTES = 15
SENTENCE_END = (".", "?", "!")

st.set_page_config(page_title="Shorts Maker", page_icon="🎬", layout="centered")


# ------------------------------------------------------------------ helpers
def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-1500:])
    return p.stdout


def get_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip())


def download_youtube(url, outdir):
    from yt_dlp import YoutubeDL

    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(outdir / "input.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = sorted(outdir.glob("input.*"), key=lambda f: f.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was found.")
    return files[0]


@st.cache_resource(show_spinner="Loading speech model (first time takes a minute)...")
def load_model(size):
    from faster_whisper import WhisperModel

    return WhisperModel(size, device="cpu", compute_type="int8")


def transcribe(model, path, duration, translate, on_progress):
    segs, _ = model.transcribe(
        str(path),
        word_timestamps=True,
        vad_filter=True,
        beam_size=1,
        task="translate" if translate else "transcribe",
        language=None if translate else "en",
    )
    segments, words = [], []
    for s in segs:
        segments.append((s.start, s.end))
        for w in s.words or []:
            text = w.word.strip().replace("{", "").replace("}", "")
            if text:
                words.append({"text": text, "start": w.start, "end": w.end})
        on_progress(min(s.end / duration, 1.0))
    return segments, words


def plan_clips(segments, duration, max_len):
    """Group transcript segments into clips of at most max_len seconds,
    always cutting between sentences/segments."""
    if not segments:
        n = int(duration // max_len) + (1 if duration % max_len > 5 else 0)
        return [(i * max_len, min((i + 1) * max_len, duration)) for i in range(max(n, 1))]

    clips = []
    cur_start, cur_end = segments[0]
    for s, e in segments[1:]:
        if e - cur_start <= max_len:
            cur_end = e
        else:
            clips.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    clips.append((cur_start, cur_end))

    final = []
    for s, e in clips:
        s = max(s - 0.2, 0)
        e = min(min(e + 0.4, s + max_len), duration)
        if e - s >= 5:
            final.append((s, e))
    return final


# ------------------------------------------------------------------ captions
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,74,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,430,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def chunk_words(words, n):
    chunk = []
    for w in words:
        chunk.append(w)
        if len(chunk) >= n or w["text"].endswith(SENTENCE_END):
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def build_ass(words, words_per_line, uppercase):
    lines = [ASS_HEADER]
    for chunk in chunk_words(words, words_per_line):
        for j, w in enumerate(chunk):
            start = w["start"]
            end = chunk[j + 1]["start"] if j + 1 < len(chunk) else w["end"]
            if end <= start:
                end = start + 0.05
            parts = []
            for k, c in enumerate(chunk):
                t = c["text"].upper() if uppercase else c["text"]
                if k == j:
                    parts.append(r"{\c&H00FFFF&}" + t + r"{\c&HFFFFFF&}")
                else:
                    parts.append(t)
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{' '.join(parts)}"
            )
    return "\n".join(lines) + "\n"


def render_clip(src, start, end, words, workdir, idx, blur_bg, words_per_line, uppercase):
    length = end - start
    rel = [
        {"text": w["text"], "start": max(w["start"] - start, 0), "end": min(w["end"] - start, length)}
        for w in words
        if w["end"] > start and w["start"] < end
    ]
    ass_name = f"sub{idx}.ass"
    (workdir / ass_name).write_text(build_ass(rel, words_per_line, uppercase), encoding="utf-8")
    out = workdir / f"short_{idx}.mp4"

    cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src)]
    if blur_bg:
        fc = (
            "[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
            "[b]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass={ass_name}[v]"
        )
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
    else:
        vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,ass={ass_name}"
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
    ]
    run(cmd, cwd=workdir)
    return out


# ------------------------------------------------------------------ UI
st.title("🎬 Shorts Maker")
st.caption("Turn a long video into ~1 minute vertical clips with English captions.")

tab_up, tab_url = st.tabs(["Upload video", "YouTube link"])
with tab_up:
    uploaded = st.file_uploader("Video file", type=["mp4", "mov", "mkv", "webm", "m4v"])
with tab_url:
    url = st.text_input("YouTube URL")
    st.caption("YouTube often blocks downloads from cloud servers. If the link fails, "
               "download the video yourself and use the Upload tab.")

with st.expander("Settings"):
    max_len = st.slider("Max clip length (seconds)", 20, 60, 60, 5)
    model_size = st.selectbox("Speech model", ["tiny", "base", "small"], index=1,
                              help="Bigger = more accurate but slower. 'base' is a good start on free hosting.")
    lang = st.radio("Spoken language", ["English", "Other (translate to English)"], horizontal=True)
    fit = st.radio("Framing", ["Center crop (fills screen)", "Blurred background (shows full frame)"])
    words_per_line = st.slider("Words per caption line", 1, 6, 3)
    uppercase = st.checkbox("UPPERCASE captions", value=True)

owns = st.checkbox("I own this video or have permission to use it.")
go = st.button("Create shorts", type="primary", disabled=not owns)

if go:
    if not uploaded and not url.strip():
        st.error("Upload a video or paste a YouTube link first.")
        st.stop()

    st.session_state.pop("clips", None)
    workdir = Path(tempfile.mkdtemp())
    try:
        # 1. get the video
        if uploaded:
            src = workdir / ("input" + Path(uploaded.name).suffix)
            src.write_bytes(uploaded.getbuffer())
        else:
            with st.spinner("Downloading video..."):
                try:
                    src = download_youtube(url.strip(), workdir)
                except Exception as e:
                    st.error("Could not download that link (YouTube often blocks cloud servers). "
                             "Please download the video and use the Upload tab.")
                    st.caption(str(e)[:300])
                    st.stop()

        duration = get_duration(src)
        if duration > MAX_MINUTES * 60:
            st.error(f"Video is {duration/60:.1f} min. This free app is limited to {MAX_MINUTES} minutes.")
            st.stop()

        # 2. transcribe
        model = load_model(model_size)
        bar = st.progress(0.0, text="Transcribing...")
        segments, words = transcribe(
            model, src, duration, lang != "English",
            lambda p: bar.progress(p, text=f"Transcribing... {int(p*100)}%"),
        )

        # 3. plan clips
        plan = plan_clips(segments, duration, max_len)
        if not words:
            st.warning("No speech detected, so clips are cut at fixed intervals without captions.")

        # 4. render
        clips = []
        for i, (s, e) in enumerate(plan, 1):
            bar.progress((i - 1) / len(plan), text=f"Rendering clip {i} of {len(plan)}...")
            out = render_clip(src, s, e, words, workdir, i, fit.startswith("Blurred"),
                              words_per_line, uppercase)
            clips.append({"name": f"short_{i}.mp4", "data": out.read_bytes(), "start": s, "end": e})
        bar.progress(1.0, text="Done!")
        st.session_state["clips"] = clips
    except RuntimeError as e:
        st.error("Processing failed.")
        st.code(str(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

clips = st.session_state.get("clips")
if clips:
    st.subheader(f"{len(clips)} clips ready")
    cols = st.columns(3)
    for i, c in enumerate(clips):
        with cols[i % 3]:
            st.video(c["data"])
            st.caption(f"{c['start']:.0f}s to {c['end']:.0f}s ({c['end'] - c['start']:.0f}s)")
            st.download_button("Download", c["data"], file_name=c["name"],
                               mime="video/mp4", key=f"dl_{i}")
