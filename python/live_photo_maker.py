"""
Live Photo Maker - turn any video into an iOS Live Photo (JPG + MOV pair).

An iOS Live Photo is two files that share a "content identifier" UUID:
  * a JPEG whose Apple MakerNote (tag 0x0011) holds the UUID
  * a QuickTime MOV with the same UUID in com.apple.quicktime.content.identifier
    plus a timed-metadata track marking the key photo (still-image-time)

Requires ffmpeg/ffprobe on PATH. No Python packages needed.

Usage:
  python live_photo_maker.py                 -> opens the GUI
  python live_photo_maker.py video.mp4 [...] -> command line (see --help)
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # hide ffmpeg console windows


# ---------------------------------------------------------------- ffmpeg ---

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{p.stderr[-1500:]}")
    return p.stdout


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


# Long side capped at 1920px, dimensions kept even for H.264.
SCALE = "scale='if(gt(iw,ih),min(1920,iw),-2)':'if(gt(iw,ih),-2,min(1920,ih))'"


def encode_mov(src, dst, start, length, content_id, mute):
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-t", f"{length}",
           "-i", str(src), "-map", "0:v:0"]
    cmd += ["-an"] if mute else ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-map_metadata", "-1", "-vf", SCALE, "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
            "-metadata", f"com.apple.quicktime.content.identifier={content_id}",
            "-movflags", "use_metadata_tags", "-f", "mov", str(dst)]
    run(cmd)


def extract_frame(mov, dst, at):
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at}", "-i", str(mov),
         "-frames:v", "1", "-q:v", "2", "-update", "1", str(dst)])


# ------------------------------------------------------------------ JPEG ---

def apple_makernote(content_id):
    # "Apple iOS\0" + version 1 + "MM"; offsets are relative to makernote start.
    header = b"Apple iOS\x00" + b"\x00\x01" + b"MM"
    cid = content_id.encode("ascii") + b"\x00"
    n = 2
    data_off = len(header) + 2 + n * 12 + 4
    ifd = struct.pack(">H", n)
    ifd += struct.pack(">HHIi", 0x0001, 9, 1, 11)                      # MakerNoteVersion
    ifd += struct.pack(">HHII", 0x0011, 2, len(cid), data_off)         # ContentIdentifier
    ifd += struct.pack(">I", 0)
    return header + ifd + cid


def exif_app1(content_id):
    make = b"Apple\x00"
    mn = apple_makernote(content_id)
    ifd0_off = 8
    make_off = ifd0_off + 2 + 2 * 12 + 4
    exif_off = make_off + len(make)
    mn_off = exif_off + 2 + 12 + 4
    tiff = b"MM\x00\x2a" + struct.pack(">I", ifd0_off)
    tiff += struct.pack(">H", 2)
    tiff += struct.pack(">HHII", 0x010F, 2, len(make), make_off)       # Make
    tiff += struct.pack(">HHII", 0x8769, 4, 1, exif_off)               # ExifIFD pointer
    tiff += struct.pack(">I", 0) + make
    tiff += struct.pack(">H", 1)
    tiff += struct.pack(">HHII", 0x927C, 7, len(mn), mn_off)           # MakerNote
    tiff += struct.pack(">I", 0) + mn
    payload = b"Exif\x00\x00" + tiff
    return b"\xFF\xE1" + struct.pack(">H", len(payload) + 2) + payload


def tag_jpeg(path, content_id):
    data = Path(path).read_bytes()
    if data[:2] != b"\xFF\xD8":
        raise RuntimeError("not a JPEG")
    pos = 2
    if data[2:4] == b"\xFF\xE0":  # keep JFIF APP0 first
        pos += 2 + struct.unpack(">H", data[4:6])[0]
    Path(path).write_bytes(data[:pos] + exif_app1(content_id) + data[pos:])


# ------------------------------------------------------------------- MOV ---

def box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full(kind, payload, version=0, flags=0):
    return box(kind, struct.pack(">I", (version << 24) | flags) + payload)


def children(data, start=0, end=None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        yield kind, pos, pos + hdr, pos + size
        pos += size


def still_image_trak(track_id, timescale, movie_dur, still, chunk_offset):
    still = max(0, min(still, movie_dur - 1))
    sample_dur = max(1, movie_dur - still)
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = full(b"tkhd", struct.pack(">IIIII", 0, 0, track_id, 0, movie_dur)
                + b"\x00" * 8 + struct.pack(">hhhH", 0, 0, 0, 0) + matrix
                + struct.pack(">II", 0, 0), flags=1)
    entries = []
    if still > 0:
        entries.append(struct.pack(">IiI", still, -1, 0x10000))      # empty edit
    entries.append(struct.pack(">IiI", sample_dur, 0, 0x10000))
    edts = box(b"edts", full(b"elst", struct.pack(">I", len(entries)) + b"".join(entries)))

    mdhd = full(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, sample_dur, 0x55C4, 0))
    hdlr = full(b"hdlr", b"mhlr" + b"meta" + b"\x00" * 12 + b"\x00")
    keyd = box(b"keyd", b"mdta" + b"com.apple.quicktime.still-image-time")
    dtyp = box(b"dtyp", struct.pack(">II", 0, 65))                    # well-known int8
    keys = box(b"keys", box(struct.pack(">I", 1), keyd + dtyp))
    mebx = box(b"mebx", b"\x00" * 6 + struct.pack(">H", 1) + keys)
    stbl = box(b"stbl",
               full(b"stsd", struct.pack(">I", 1) + mebx)
               + full(b"stts", struct.pack(">III", 1, 1, sample_dur))
               + full(b"stsc", struct.pack(">IIII", 1, 1, 1, 1))
               + full(b"stsz", struct.pack(">III", 0, 1, 9))
               + full(b"stco", struct.pack(">II", 1, chunk_offset)))
    dinf = box(b"dinf", full(b"dref", struct.pack(">I", 1) + full(b"url ", b"", flags=1)))
    minf = box(b"minf", full(b"nmhd", b"") + dinf + stbl)
    mdia = box(b"mdia", mdhd + hdlr + minf)
    return box(b"trak", tkhd + edts + mdia)


def add_still_image_track(path, still_seconds):
    data = Path(path).read_bytes()
    top = list(children(data))
    moov = next((b for b in top if b[0] == b"moov"), None)
    if moov is None or moov[1] < next(b for b in top if b[0] == b"mdat")[1]:
        raise RuntimeError("unexpected MOV layout (moov must follow mdat)")
    _, m_start, m_body, m_end = moov

    parts, timescale, movie_dur, last_trak = [], 0, 0, -1
    for kind, s, b, e in children(data, m_body, m_end):
        chunk = bytearray(data[s:e])
        if kind == b"mvhd":
            ver = data[b]
            if ver == 1:
                timescale, movie_dur = struct.unpack(">IQ", data[b + 20:b + 32])
            else:
                timescale, movie_dur = struct.unpack(">II", data[b + 12:b + 20])
            next_id = struct.unpack(">I", chunk[-4:])[0]
            chunk[-4:] = struct.pack(">I", next_id + 1)
        if kind == b"trak":
            last_trak = len(parts)
        parts.append(bytes(chunk))

    sample = struct.pack(">II", 9, 1) + b"\xFF"     # key 1 (still-image-time) = -1
    mdat2 = box(b"mdat", sample)
    trak = still_image_trak(next_id, timescale, movie_dur,
                            round(still_seconds * timescale), m_start + 8)
    parts.insert(last_trak + 1, trak)
    new_moov = box(b"moov", b"".join(parts))
    Path(path).write_bytes(data[:m_start] + mdat2 + new_moov + data[m_end:])


# ------------------------------------------------------------- converter ---

def convert(src, out_dir, start=0.0, length=3.0, key_at=None, mute=False):
    """Convert one video. key_at = key-photo time in seconds within the clip
    (default: middle). Returns (jpg_path, mov_path)."""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = probe_duration(src)
    start = max(0.0, min(start, total - 0.1))
    length = max(0.5, min(length, total - start))
    key_at = length / 2 if key_at is None else max(0.0, min(key_at, length - 0.05))

    content_id = str(uuid.uuid4()).upper()
    jpg = out_dir / f"{src.stem}.JPG"
    mov = out_dir / f"{src.stem}.MOV"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_mov = Path(tmp) / "clip.mov"
        tmp_jpg = Path(tmp) / "still.jpg"
        encode_mov(src, tmp_mov, start, length, content_id, mute)
        add_still_image_track(tmp_mov, key_at)
        extract_frame(tmp_mov, tmp_jpg, key_at)
        tag_jpeg(tmp_jpg, content_id)
        shutil.move(tmp_mov, mov)
        shutil.move(tmp_jpg, jpg)
    return jpg, mov


# ------------------------------------------------------------------- GUI ---

def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Live Photo Maker")
    root.geometry("620x480")
    root.minsize(520, 400)
    files = []

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Videos").grid(row=0, column=0, sticky="w")
    lst = tk.Listbox(frm, height=7)
    lst.grid(row=1, column=0, columnspan=3, sticky="nsew")

    def add_files():
        for f in filedialog.askopenfilenames(filetypes=[
                ("Videos", "*.mp4 *.mov *.m4v *.mkv *.avi *.webm *.gif"), ("All", "*.*")]):
            if f not in files:
                files.append(f)
                lst.insert("end", f)
        if files and not out_var.get():
            out_var.set(str(Path(files[0]).parent / "LivePhotos"))

    def clear():
        files.clear()
        lst.delete(0, "end")

    btns = ttk.Frame(frm)
    btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 10))
    ttk.Button(btns, text="Add videos…", command=add_files).pack(side="left")
    ttk.Button(btns, text="Clear", command=clear).pack(side="left", padx=6)

    out_var = tk.StringVar()
    ttk.Label(frm, text="Output folder").grid(row=3, column=0, sticky="w")
    ttk.Entry(frm, textvariable=out_var).grid(row=3, column=1, sticky="ew", padx=6)
    ttk.Button(frm, text="Browse…",
               command=lambda: out_var.set(filedialog.askdirectory() or out_var.get())
               ).grid(row=3, column=2)

    opts = ttk.Frame(frm)
    opts.grid(row=4, column=0, columnspan=3, sticky="w", pady=10)
    start_var = tk.DoubleVar(value=0.0)
    len_var = tk.DoubleVar(value=3.0)
    key_var = tk.StringVar(value="middle")
    mute_var = tk.BooleanVar(value=False)
    ttk.Label(opts, text="Start (s)").pack(side="left")
    ttk.Spinbox(opts, from_=0, to=9999, increment=0.5, width=6,
                textvariable=start_var).pack(side="left", padx=(4, 12))
    ttk.Label(opts, text="Length (s)").pack(side="left")
    ttk.Spinbox(opts, from_=0.5, to=10, increment=0.5, width=5,
                textvariable=len_var).pack(side="left", padx=(4, 12))
    ttk.Label(opts, text="Key photo at (s)").pack(side="left")
    ttk.Entry(opts, width=7, textvariable=key_var).pack(side="left", padx=(4, 12))
    ttk.Checkbutton(opts, text="No sound", variable=mute_var).pack(side="left")

    log = tk.Text(frm, height=8, state="disabled")
    log.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(8, 0))

    def write(msg):
        def _():
            log.configure(state="normal")
            log.insert("end", msg + "\n")
            log.see("end")
            log.configure(state="disabled")
        root.after(0, _)

    def start():
        if not files:
            messagebox.showinfo("Live Photo Maker", "Add at least one video first.")
            return
        if not shutil.which("ffmpeg"):
            messagebox.showerror("Live Photo Maker",
                                 "ffmpeg not found. Install it with:\nwinget install ffmpeg")
            return
        try:
            s, l = float(start_var.get()), float(len_var.get())
            k = None if key_var.get().strip().lower() in ("", "middle") else float(key_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Live Photo Maker", "Start, length and key photo must be numbers.")
            return
        out, mute = out_var.get() or str(Path(files[0]).parent / "LivePhotos"), mute_var.get()
        go.configure(state="disabled")

        def work():
            ok = 0
            for f in list(files):
                write(f"Converting {Path(f).name} …")
                try:
                    jpg, mov = convert(f, out, s, l, k, mute)
                    write(f"  ✓ {jpg.name} + {mov.name}")
                    ok += 1
                except Exception as e:
                    write(f"  ✗ {e}")
            write(f"Done: {ok}/{len(files)} converted → {out}")
            root.after(0, lambda: go.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    go = ttk.Button(frm, text="Convert to Live Photos", command=start)
    go.grid(row=5, column=0, columnspan=3, sticky="ew")

    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(1, weight=1)
    frm.rowconfigure(6, weight=1)
    root.mainloop()


def cli():
    ap = argparse.ArgumentParser(description="Convert videos to iOS Live Photos (JPG + MOV).")
    ap.add_argument("videos", nargs="+")
    ap.add_argument("-o", "--out", default="LivePhotos", help="output folder")
    ap.add_argument("-s", "--start", type=float, default=0.0, help="clip start (s)")
    ap.add_argument("-l", "--length", type=float, default=3.0, help="clip length (s)")
    ap.add_argument("-k", "--key", type=float, default=None,
                    help="key photo time within the clip (s), default middle")
    ap.add_argument("--mute", action="store_true", help="drop audio")
    a = ap.parse_args()
    for v in a.videos:
        jpg, mov = convert(v, a.out, a.start, a.length, a.key, a.mute)
        print(f"{v} -> {jpg} + {mov}")


if __name__ == "__main__":
    cli() if len(sys.argv) > 1 else gui()
