#!/usr/bin/env python3
"""
Frame Extractor  -  streaming VirtualDub2-style decimation + image-sequence export.

How it works (and why it is fast on a USB hard drive)
------------------------------------------------------
Old design:   ffmpeg -> thousands of files on SSD temp -> read back -> rewrite to HDD
New design:   ffmpeg -> one pipe into RAM -> a small pool of writer threads -> HDD

* Every frame is written to the hard drive exactly once, straight from memory, while
  ffmpeg is still decoding. Extraction and disk writing overlap, so a job takes
  roughly max(decode time, write time) instead of decode + copy + delete.
* Frame numbering is assigned by this program as each JPEG arrives, so numbering is
  exact and continuous across segments (no parsing of ffmpeg's log).
* Segments can be marked inside the program (per video, with a frame preview and
  in/out points) or imported from a LosslessCut CSV, so no separate cutting step
  is needed before extraction.
* Decimation is frame-exact (keep frame 0, N, 2N ... of each segment), which is what
  VirtualDub2's "Decimate by N" does. A time-based mode is kept for VFR sources.
* With NVIDIA decoding the frames stay on the GPU; only the frames that survive
  decimation are copied back to system memory.
* No fsync: the Windows write cache is left to coalesce the small writes. A handful
  of writer threads hides per-file overhead (antivirus scans, NTFS metadata).

Requires: Python 3.9+, PyQt6, and ffmpeg/ffprobe (5.1 or newer) on PATH or next to
this script.
"""
from __future__ import annotations

import csv
import dataclasses
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "Frame Extractor"
APP_VERSION = "2.1"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW
SCRIPT_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg",
              ".vob", ".wmv", ".webm", ".flv"}

DECODERS = {
    "auto": "Auto (NVIDIA if available, else CPU)",
    "cpu": "CPU",
    "cuda": "NVIDIA (CUDA / NVDEC)",
    "d3d11va": "Any GPU (D3D11VA)",
}
METHODS = {
    "exact": "Frame-exact (VirtualDub2)",
    "time": "Time-based (variable frame rate)",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def find_tool(name: str) -> Optional[str]:
    """ffmpeg/ffprobe next to the script wins, then PATH."""
    for cand in (SCRIPT_DIR / (name + ".exe"), SCRIPT_DIR / name):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return name.strip().rstrip(".")


def default_base_name(video: str) -> str:
    stem = Path(video).stem
    return sanitize_name(re.sub(r"-cut.*|-merged.*", "", stem).strip())


def auto_name(text: str, series: int, fallback_ep: int) -> str:
    """'6 - Dalek' -> 'S1E06 Dalek'. Uses an SxxEyy or leading number already in the
    name for the episode; otherwise fallback_ep. The series number comes from the caller."""
    text = text.strip()
    ep, title = None, text
    m = re.match(r"^\s*S\d+\s*E(\d+)\s*[-_.–—:]*\s*(.*)$", text, re.IGNORECASE)
    if m:
        ep, title = int(m.group(1)), m.group(2)
    else:
        m = re.match(r"^\s*(?:ep(?:isode)?\s*)?(\d{1,3})(?!\d)\s*[-_.–—:)]*\s*(.*)$", text, re.IGNORECASE)
        if m:
            ep, title = int(m.group(1)), m.group(2)
    if ep is None:
        ep = fallback_ep
    title = re.sub(r"[_]+", " ", title).strip(" -_.")
    return sanitize_name(f"S{series}E{ep:02d} {title}".strip())


def episode_dir(root: str, base: str, organise: bool) -> Path:
    """Root\\Series N\\<base>\\ when the name starts with SxEyy and organise is on."""
    root_p = Path(root)
    if not organise:
        return root_p
    m = re.match(r"^S(\d+)E\d+", base, re.IGNORECASE)
    if m:
        return root_p / f"Series {int(m.group(1))}" / base
    return root_p / base


def frame_name(base: str, num: int, pad: int, ext: str) -> str:
    return f"{base} {num:0{pad}d}{ext}"


def fmt_duration(sec: float) -> str:
    if sec is None or sec != sec or sec < 0:
        return "–"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_tc(sec: Optional[float]) -> str:
    """Editable timecode: H:MM:SS.mmm (always shows milliseconds)."""
    if sec is None or sec != sec or sec < 0:
        return "–"
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping mid-batch (per-thread; cleared when the thread exits)."""
    if IS_WIN:
        try:
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        except Exception:
            pass


def mark_not_indexed(path: Path) -> None:
    """Stop Windows Search indexing the new frames (new files inherit this flag)."""
    if IS_WIN:
        try:
            k32 = ctypes.windll.kernel32
            attrs = k32.GetFileAttributesW(str(path))
            if attrs != 0xFFFFFFFF:
                k32.SetFileAttributesW(str(path), attrs | 0x2000)  # NOT_CONTENT_INDEXED
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Probing and LosslessCut segments
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class VideoInfo:
    fps_num: int
    fps_den: int
    avg_fps: float
    duration: float
    codec: str
    pix_fmt: str
    width: int
    height: int

    @property
    def is_10bit(self) -> bool:
        return any(t in self.pix_fmt for t in ("10", "12"))


def _frac(text: Optional[str]) -> Optional[tuple[int, int]]:
    try:
        if not text:
            return None
        n, _, d = text.partition("/")
        n, d = int(n), int(d or 1)
        return (n, d) if n > 0 and d > 0 else None
    except ValueError:
        return None


def probe_video(path: str) -> VideoInfo:
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe was not found on PATH or next to this program.")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries",
           "stream=codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,duration"
           ":format=duration",
           "-of", "json", path]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip() or r.returncode}")
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found.")
    s = streams[0]
    rate = _frac(s.get("r_frame_rate")) or _frac(s.get("avg_frame_rate")) or (25, 1)
    avg = _frac(s.get("avg_frame_rate")) or rate
    dur = 0.0
    for v in (s.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            dur = float(v)
            if dur > 0:
                break
        except (TypeError, ValueError):
            pass
    return VideoInfo(rate[0], rate[1], avg[0] / avg[1], dur, s.get("codec_name", "?"),
                     s.get("pix_fmt", ""), int(s.get("width") or 0), int(s.get("height") or 0))


def parse_time(text: str) -> Optional[float]:
    """Seconds ('754.12') or clock ('00:12:34.120' / '12:34')."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    if 2 <= len(parts) <= 3:
        try:
            total = 0.0
            for p in parts:
                total = total * 60 + float(p)
            return total
        except ValueError:
            return None
    return None


def load_segments(csv_path: str) -> list[tuple[float, Optional[float]]]:
    """LosslessCut CSV: start,end[,label]. Header rows are skipped; empty end = to EOF."""
    segs: list[tuple[float, Optional[float]]] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row:
                continue
            start = parse_time(row[0])
            if start is None:
                continue                      # header or junk
            end = parse_time(row[1]) if len(row) > 1 else None
            if end is not None and end <= start:
                continue
            segs.append((start, end))
    return segs


Segs = list[tuple[float, Optional[float]]]


def save_segments(csv_path: str, segs: Segs) -> None:
    """Write segments in LosslessCut's CSV layout (start,end,label) so they can be
    opened there too, or re-attached to this program later."""
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for i, (st, en) in enumerate(segs, 1):
            w.writerow([f"{st:.3f}", "" if en is None else f"{en:.3f}", f"Segment {i}"])


def tidy_segments(segs: Segs, duration: float = 0.0) -> Segs:
    """Sorted by start, clamped to the video, with empty/inverted ranges dropped.
    An end at (or past) the end of the video becomes 'to EOF' (None)."""
    out: Segs = []
    for st, en in segs:
        st = max(0.0, float(st))
        if en is not None:
            en = float(en)
            if duration > 0 and en >= duration - 0.0005:
                en = None
        if en is not None and en <= st + 0.0005:
            continue
        if duration > 0 and st >= duration - 0.0005:
            continue
        out.append((round(st, 3), None if en is None else round(en, 3)))
    out.sort(key=lambda x: x[0])
    return out


def segments_total(segs: Segs, duration: float) -> float:
    return sum(((e if e is not None else duration) - st) for st, e in segs)


def estimate_frames(info: VideoInfo, segs, decimation: int) -> int:
    total = segments_total(segs, info.duration)
    return max(1, int(total * info.avg_fps / max(1, decimation) + 0.999))


def find_csv_for(video: str) -> Optional[str]:
    """A CSV sitting next to the video whose name starts with the video's name."""
    v = Path(video)
    try:
        cands = sorted(p for p in v.parent.iterdir()
                       if p.suffix.lower() == ".csv" and p.stem.startswith(v.stem))
    except OSError:
        return None
    return str(cands[0]) if cands else None


def grab_frame(video: str, t: float, width: int = 960, timeout: float = 20.0) -> Optional[bytes]:
    """One JPEG of the frame at time t (accurate seek), scaled to `width`, or None."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "2",
           "-ss", f"{max(0.0, t):.3f}", "-i", video, "-map", "0:v:0", "-an", "-sn", "-dn",
           "-frames:v", "1", "-vf", f"scale={int(width)}:-2:flags=fast_bilinear",
           "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


# ─────────────────────────────────────────────────────────────────────────────
#  Splitting ffmpeg's image2pipe stream into individual files
# ─────────────────────────────────────────────────────────────────────────────
class JpegSplitter:
    """Walks JPEG markers properly (header segments by length, entropy data by
    marker scan) so a 0xFFD9 inside a quantisation table can never cut a frame."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        out = []
        while True:
            end = self._frame_end()
            if end is None:
                return out
            out.append(bytes(self.buf[:end]))
            del self.buf[:end]

    def _frame_end(self) -> Optional[int]:
        b = self.buf
        if len(b) < 4:
            return None
        if b[0] != 0xFF or b[1] != 0xD8:
            i = b.find(b"\xff\xd8")
            if i < 0:
                del b[:-1]
                return None
            del b[:i]
        n = len(b)
        pos = 2
        while True:
            if pos + 2 > n:
                return None
            if b[pos] != 0xFF:
                raise ValueError("Corrupt JPEG stream from ffmpeg")
            m = b[pos + 1]
            if m == 0xFF:                       # fill byte
                pos += 1
                continue
            if m == 0xD9:                       # EOI
                return pos + 2
            if 0xD0 <= m <= 0xD7 or m == 0x01:  # standalone markers
                pos += 2
                continue
            if pos + 4 > n:
                return None
            pos += 2 + ((b[pos + 2] << 8) | b[pos + 3])
            if m == 0xDA:                       # SOS -> entropy-coded data
                while True:
                    j = b.find(b"\xff", pos)
                    if j < 0 or j + 1 >= n:
                        return None
                    nxt = b[j + 1]
                    if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                        pos = j + 2
                    elif nxt == 0xFF:
                        pos = j + 1
                    else:
                        pos = j
                        break


class PngSplitter:
    SIG = b"\x89PNG\r\n\x1a\n"

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        out = []
        while True:
            b = self.buf
            if len(b) < 8:
                return out
            if b[:8] != self.SIG:
                raise ValueError("Corrupt PNG stream from ffmpeg")
            pos, n, end = 8, len(b), None
            while pos + 8 <= n:
                length = int.from_bytes(b[pos:pos + 4], "big")
                ctype = bytes(b[pos + 4:pos + 8])
                pos += 12 + length
                if ctype == b"IEND":
                    end = pos if pos <= n else None
                    break
            if end is None:
                return out
            out.append(bytes(b[:end]))
            del b[:end]


# ─────────────────────────────────────────────────────────────────────────────
#  Jobs, settings and the disk writer
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Settings:
    output_root: str = ""
    organise: bool = True
    decimation: int = 12
    method: str = "exact"
    start_number: int = 1
    padding: int = 5
    ext: str = ".jpeg"
    quality: int = 2
    decoder: str = "auto"
    writer_threads: int = 4
    buffer_mb: int = 1024


@dataclass
class Job:
    video: str
    base_name: str
    csv_path: Optional[str] = None
    cuts: list = field(default_factory=list)     # segments marked in the program: [(start, end|None)]
    # runtime
    state: str = "queued"      # queued probing extracting flushing done error cancelled
    out_dir: str = ""
    expected: int = 0
    extracted: int = 0
    written: int = 0
    pending: int = 0
    extract_done: bool = False
    error: str = ""
    ff_fps: float = 0.0
    ff_speed: str = ""
    t_start: float = 0.0
    t_end: float = 0.0
    info: Optional[VideoInfo] = None
    overrides: dict = field(default_factory=dict)     # per-item settings (Settings field -> value)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def segments(self) -> list[tuple[float, Optional[float]]]:
        """In-app cuts win, then an attached CSV, else the whole video."""
        if self.cuts:
            return [tuple(c) for c in self.cuts]
        if self.csv_path and os.path.isfile(self.csv_path):
            segs = load_segments(self.csv_path)
            if segs:
                return segs
        return [(0.0, None)]

    def seg_source(self) -> str:
        """'cuts', 'csv' or 'whole' – where segments() comes from."""
        if self.cuts:
            return "cuts"
        if self.csv_path and os.path.isfile(self.csv_path) and load_segments(self.csv_path):
            return "csv"
        return "whole"

    def reset(self) -> None:
        with self.lock:
            self.state, self.out_dir, self.error = "queued", "", ""
            self.extracted = self.written = self.pending = 0
            self.extract_done, self.ff_fps, self.ff_speed = False, 0.0, ""
            self.t_start = self.t_end = 0.0

    # called from writer threads
    def _written(self, ok: bool) -> None:
        with self.lock:
            self.pending -= 1
            if ok:
                self.written += 1
            if self.extract_done and self.pending == 0 and self.state == "flushing":
                self._settle()

    def _settle(self) -> None:          # caller holds the lock
        self.t_end = time.time()
        if self.written >= self.extracted:
            self.state = "done"
        else:
            self.state = "error"
            self.error = f"{self.extracted - self.written} frame(s) failed to write to disk"

    def finish_extract(self) -> None:
        with self.lock:
            self.extract_done = True
            if self.pending == 0:
                self._settle()
            else:
                self.state = "flushing"


PER_ITEM_FIELDS = ("output_root", "organise", "decimation", "method",
                   "start_number", "padding", "ext", "quality")


def effective_settings(defaults: Settings, job: Job) -> Settings:
    """The defaults with this item's own overrides applied on top."""
    return dataclasses.replace(defaults, **{k: v for k, v in job.overrides.items()
                                            if k in PER_ITEM_FIELDS})


class DiskWriter:
    """RAM-budgeted queue + a few writer threads. One os.open/os.write/os.close per
    file, no fsync: the OS write-back cache turns the stream into efficient HDD I/O."""

    FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_SEQUENTIAL", 0))

    def __init__(self, threads: int, budget_bytes: int) -> None:
        self.q: queue.Queue = queue.Queue()
        self.cv = threading.Condition()
        self.budget = max(budget_bytes, 16 << 20)
        self.inflight = 0
        self.files_written = 0
        self.bytes_written = 0
        self.error: Optional[BaseException] = None
        self.cancelled = False
        self.threads = [threading.Thread(target=self._run, daemon=True, name=f"writer{i}")
                        for i in range(max(1, threads))]
        for t in self.threads:
            t.start()

    def submit(self, job: Job, path: str, data: bytes) -> None:
        n = len(data)
        with self.cv:
            while (self.inflight and self.inflight + n > self.budget
                   and not self.cancelled and self.error is None):
                self.cv.wait(0.25)
            if self.error is not None:
                raise RuntimeError(f"Disk write failed: {self.error}")
            if self.cancelled:
                return
            self.inflight += n
        with job.lock:
            job.pending += 1
        self.q.put((job, path, data))

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            job, path, data = item
            ok = False
            try:
                if not self.cancelled and self.error is None:
                    fd = os.open(path, self.FLAGS, 0o666)
                    try:
                        mv = memoryview(data)
                        while mv:
                            mv = mv[os.write(fd, mv):]
                    finally:
                        os.close(fd)
                    ok = True
            except OSError as e:
                with self.cv:
                    if self.error is None:
                        self.error = e
            finally:
                with self.cv:
                    self.inflight -= len(data)
                    if ok:
                        self.files_written += 1
                        self.bytes_written += len(data)
                    self.cv.notify_all()
                job._written(ok)

    @property
    def buffer_fill(self) -> float:
        return self.inflight / self.budget

    def cancel(self) -> None:
        with self.cv:
            self.cancelled = True
            self.cv.notify_all()

    def close(self) -> None:
        for _ in self.threads:
            self.q.put(None)
        for t in self.threads:
            t.join()


# ─────────────────────────────────────────────────────────────────────────────
#  Engine: runs the queue on a background thread; the GUI polls it
# ─────────────────────────────────────────────────────────────────────────────
class Engine:
    READ_CHUNK = 1 << 20

    def __init__(self, jobs: list[Job], settings: Settings) -> None:
        self.jobs = jobs
        self.s = settings
        self.log_q: queue.Queue[str] = queue.Queue()
        self.cancel_ev = threading.Event()
        self.finished = threading.Event()
        self.writer: Optional[DiskWriter] = None
        self.t_start = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self.cur: Settings = settings          # effective settings of the job being extracted
        self._thread = threading.Thread(target=self._run, daemon=True, name="engine")

    # public ----------------------------------------------------------------
    def start(self) -> None:
        self.t_start = time.time()
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_ev.set()
        if self.writer:
            self.writer.cancel()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.finished.wait(timeout)

    def log(self, msg: str) -> None:
        self.log_q.put(time.strftime("%H:%M:%S  ") + msg)

    # internals -------------------------------------------------------------
    def _run(self) -> None:
        keep_awake(True)
        self.writer = DiskWriter(self.s.writer_threads, self.s.buffer_mb << 20)
        try:
            for job in self.jobs:
                if self.cancel_ev.is_set():
                    break
                if job.state != "queued":
                    continue
                try:
                    self._do_job(job)
                except Exception as e:  # noqa: BLE001
                    with job.lock:
                        job.state = "cancelled" if self.cancel_ev.is_set() else "error"
                        job.error = str(e)
                        job.t_end = time.time()
                    if not self.cancel_ev.is_set():
                        self.log(f"✖ {job.base_name}: {e}")
                    if self.writer.error is not None:
                        self.log("Stopping the queue because the drive reported an error.")
                        break
            if not self.cancel_ev.is_set():
                self.log("Finishing writes to disk…")
        finally:
            self.writer.close()
            for job in self.jobs:
                with job.lock:
                    if job.state in ("probing", "extracting", "flushing"):
                        if self.cancel_ev.is_set():
                            job.state = "cancelled"
                        elif job.state == "flushing" and job.pending == 0:
                            job._settle()
                        elif self.writer.error is not None:
                            job.state, job.error = "error", f"Disk write failed: {self.writer.error}"
                        job.t_end = job.t_end or time.time()
            keep_awake(False)
            self.finished.set()

    def _do_job(self, job: Job) -> None:
        s = self.cur = effective_settings(self.s, job)
        if not s.output_root:
            raise RuntimeError("No frames folder set for this item")
        job.t_start = time.time()
        job.state = "probing"
        info = job.info = probe_video(job.video)
        segs = job.segments()
        job.expected = estimate_frames(info, segs, s.decimation)
        out_dir = episode_dir(s.output_root, job.base_name, s.organise)
        out_dir.mkdir(parents=True, exist_ok=True)
        mark_not_indexed(out_dir)
        job.out_dir = str(out_dir)
        source = job.seg_source()
        seg_txt = ("whole video" if source == "whole" else
                   f"{len(segs)} segment(s) " + ("marked in the program" if source == "cuts" else "from CSV")
                   + f" ({fmt_duration(segments_total(segs, info.duration))} of video)")
        self.log(f"▶ {job.base_name}  —  {info.codec} {info.width}×{info.height} "
                 f"@ {info.fps_num}/{info.fps_den} fps, {seg_txt}, keep 1/{s.decimation}, "
                 f"≈{job.expected} frames "
                 f"→ {out_dir}")
        job.state = "extracting"
        num = s.start_number
        for i, (st, en) in enumerate(segs, 1):
            if self.cancel_ev.is_set():
                raise RuntimeError("Cancelled")
            if len(segs) > 1:
                end_txt = f"{en:.3f}s" if en is not None else "end"
                self.log(f"   segment {i}/{len(segs)}: {st:.3f}s → {end_txt}")
            num = self._segment(job, info, st, en, num, out_dir)
        if self.cancel_ev.is_set():
            raise RuntimeError("Cancelled")
        job.finish_extract()
        el = time.time() - job.t_start
        self.log(f"✔ {job.base_name}: {job.extracted} frames extracted in {fmt_duration(el)}"
                 + ("" if job.state == "done" else " (still writing to disk)"))

    def _segment(self, job, info, start, end, num, out_dir) -> int:
        decoder = self.s.decoder
        if decoder == "auto":
            # once the GPU has failed in this run, don't keep retrying it
            attempts = ["cpu"] if getattr(self, "_gpu_failed", False) else ["cuda", "cpu"]
        else:
            attempts = [decoder] + (["cpu"] if decoder != "cpu" else [])
        last_err = ""
        for dec in attempts:
            produced, rc, tail = self._ffmpeg(job, info, start, end, num, out_dir, dec)
            if self.cancel_ev.is_set() or rc == 0:
                return num + produced
            last_err = tail
            if produced == 0 and dec != "cpu":
                self._gpu_failed = True
                reason = tail.splitlines()[0] if tail else f"exit code {rc}"
                self.log(f"   {DECODERS[dec]} decoding unavailable, using CPU instead ({reason})")
                continue
            break
        raise RuntimeError(f"ffmpeg failed: {last_err.strip().splitlines()[-1] if last_err else '?'}")

    def build_cmd(self, info: VideoInfo, start: float, end: Optional[float], dec: str) -> list[str]:
        s = self.s
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg was not found on PATH or next to this program.")
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-progress", "pipe:2", "-nostats"]
        if dec == "cuda":
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        elif dec == "d3d11va":
            cmd += ["-hwaccel", "d3d11va"]
        if start > 0:
            cmd += ["-ss", f"{start:.6f}"]
        if end is not None:
            cmd += ["-t", f"{end - start:.6f}"]
        return cmd

    def _ffmpeg(self, job, info, start, end, num, out_dir, dec) -> tuple[int, int, str]:
        s = self.cur
        cmd = self.build_cmd(info, start, end, dec)
        cmd += ["-i", job.video, "-map", "0:v:0", "-an", "-sn", "-dn"]
        chain = []
        if s.decimation > 1:
            if s.method == "exact":
                chain.append(f"select=not(mod(n\\,{s.decimation}))")
            else:
                chain.append(f"fps={info.fps_num}/{info.fps_den * s.decimation}")
        if dec == "cuda":
            chain += ["hwdownload", "format=p010le" if info.is_10bit else "format=nv12"]
        if chain:
            cmd += ["-vf", ",".join(chain)]
        cmd += ["-fps_mode", "passthrough"]
        png = s.ext.lower() == ".png"
        if png:
            cmd += ["-c:v", "png", "-f", "image2pipe", "pipe:1"]
            splitter = PngSplitter()
        else:
            cmd += ["-c:v", "mjpeg", "-q:v", str(s.quality), "-f", "image2pipe", "pipe:1"]
            splitter = JpegSplitter()

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, bufsize=0, creationflags=NO_WINDOW)
        self._proc = proc
        tail: deque[str] = deque(maxlen=12)

        def read_stderr() -> None:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                k, eq, v = line.partition("=")
                if eq and re.fullmatch(r"[a-z0-9_]+", k):          # -progress key=value
                    if k == "fps":
                        try:
                            job.ff_fps = float(v)
                        except ValueError:
                            pass
                    elif k == "speed":
                        job.ff_speed = v
                elif line:
                    tail.append(line)
                    if dec == "cpu":
                        self.log(f"   ffmpeg: {line}")

        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_err.start()

        produced = 0
        pad, ext, base = s.padding, s.ext, job.base_name
        out = str(out_dir)
        try:
            while True:
                chunk = proc.stdout.read(self.READ_CHUNK)
                if not chunk:
                    break
                for frame in splitter.feed(chunk):
                    path = os.path.join(out, frame_name(base, num + produced, pad, ext))
                    self.writer.submit(job, path, frame)
                    produced += 1
                    with job.lock:
                        job.extracted += 1
                if self.cancel_ev.is_set():
                    proc.kill()
                    break
        except Exception:
            proc.kill()
            raise
        finally:
            proc.stdout.close()
            rc = proc.wait()
            t_err.join(timeout=5)
            proc.stderr.close()
            self._proc = None
        return produced, rc, "\n".join(tail)


def benchmark_drive(folder: str, threads: int, files: int = 1000, size: int = 150_000,
                    progress: Optional[Callable[[int], None]] = None) -> tuple[float, float]:
    """Write `files` incompressible frame-sized files into a scratch folder on the
    target drive, time it, then delete them. Returns (files/s, MB/s)."""
    test_dir = Path(folder) / f"_frame_extractor_speedtest_{os.getpid()}"
    test_dir.mkdir(parents=True, exist_ok=True)
    mark_not_indexed(test_dir)
    payloads = [os.urandom(size) for _ in range(8)]
    dummy = Job(video="", base_name="speedtest")
    w = DiskWriter(threads, 512 << 20)
    t0 = time.perf_counter()
    try:
        for i in range(files):
            w.submit(dummy, str(test_dir / f"speedtest {i:05d}.jpeg"), payloads[i % 8])
            if progress and i % 50 == 0:
                progress(i)
    finally:
        w.close()
    el = max(time.perf_counter() - t0, 1e-6)
    err = w.error
    shutil.rmtree(test_dir, ignore_errors=True)
    if err:
        raise RuntimeError(str(err))
    return files / el, files * size / el / 1e6


# ═════════════════════════════════════════════════════════════════════════════
#  GUI
# ═════════════════════════════════════════════════════════════════════════════
from PyQt6.QtCore import (Qt, QItemSelectionModel, QObject, QPointF, QRectF, QSettings,  # noqa: E402
                          QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QColor, QCursor, QFont, QDesktopServices, QIcon, QKeySequence,  # noqa: E402
                         QPainter, QPalette, QPen, QPixmap, QPolygonF, QShortcut)
from PyQt6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication,  # noqa: E402
                             QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                             QSizePolicy, QSlider, QSpinBox, QTableWidget, QTableWidgetItem,
                             QTextBrowser, QVBoxLayout, QWidget)

ACCENT = "#4c8dff"
QSS = """
* { font-size: 10pt; color: #e6e8ee; }
QMainWindow, QWidget#root { background: #0e1015; }
QFrame#card { background: #161a22; border: 1px solid #242a36; border-radius: 12px; }
QFrame#tile { background: #12151c; border: 1px solid #222835; border-radius: 10px; }
QLabel { background: transparent; }
QLabel#h1 { font-size: 17pt; font-weight: 600; }
QLabel#sub, QLabel#hint, QLabel#tileCap, QLabel#tileSub { color: #8a93a6; }
QLabel#hint { font-size: 8.5pt; }
QLabel#tileCap { font-size: 8pt; font-weight: 600; letter-spacing: 1px; }
QLabel#tileVal { font-size: 15pt; font-weight: 600; }
QLabel#tileSub { font-size: 8.5pt; }
QLabel#section { color: #6f7a90; font-size: 8pt; font-weight: 700; letter-spacing: 1.5px; padding-top: 10px; }
QLabel#cardTitle { font-size: 11.5pt; font-weight: 600; }
QLabel#preview { font-family: Consolas; font-size: 9pt; color: #9ec1ff;
                 background: #0f1218; border: 1px dashed #2a3140; border-radius: 7px; padding: 8px; }
QLabel#banner { border-radius: 8px; padding: 6px 12px; font-weight: 600; }
QLabel#empty { color: #6f7a90; font-size: 11pt; }
QLineEdit, QSpinBox, QComboBox { background: #0f1218; border: 1px solid #2a3140; border-radius: 7px;
                                 padding: 6px 8px; selection-background-color: #4c8dff; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #4c8dff; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #5b6375; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #161a22; border: 1px solid #2a3140; selection-background-color: #243a66; outline: none; }
QCheckBox { spacing: 8px; }
QPushButton { background: #1f2531; border: 1px solid #2c3444; border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: #283043; }
QPushButton:pressed { background: #1a1f2a; }
QPushButton:disabled { color: #5b6375; background: #151922; border-color: #20252f; }
QPushButton#primary { background: #4c8dff; border: none; color: white; font-weight: 600; padding: 9px 24px; }
QPushButton#primary:hover { background: #6a9fff; }
QPushButton#primary:disabled { background: #25324a; color: #7d8aa3; }
QPushButton#danger { background: #34191f; border: 1px solid #5c2a35; color: #ff9aa9; font-weight: 600; padding: 9px 20px; }
QPushButton#danger:hover { background: #45202a; }
QPushButton#ghost { background: transparent; border: none; color: #9aa3b5; padding: 6px 10px; }
QPushButton#ghost:hover { color: #e6e8ee; background: #1c212b; }
QTableWidget { background: transparent; border: none; outline: none; }
QTableWidget::item { padding: 4px 6px; border-bottom: 1px solid #1f2430; }
QTableWidget::item:selected { background: #1f2d49; color: #ffffff; }
QHeaderView::section { background: transparent; color: #8a93a6; border: none; border-bottom: 1px solid #242a36;
                       padding: 6px; font-weight: 600; font-size: 9pt; }
QProgressBar { background: #0f1218; border: 1px solid #242a36; border-radius: 6px; text-align: center;
               min-height: 20px; max-height: 20px; color: #d7dce6; font-size: 8.5pt; }
QProgressBar::chunk { border-radius: 5px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3a78ff, stop:1 #78a6ff); }
QProgressBar[state="done"]::chunk { background: #2fae6a; }
QProgressBar[state="error"]::chunk { background: #d8475f; }
QProgressBar[state="flushing"]::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #c08a1e, stop:1 #f0b43c); }
QProgressBar#overall { min-height: 8px; max-height: 8px; border: none; border-radius: 4px; background: #1d222c; }
QProgressBar#overall::chunk { border-radius: 4px; }
QPlainTextEdit { background: #0b0d11; border: 1px solid #1f2430; border-radius: 8px;
                 font-family: Consolas; font-size: 9pt; color: #b9c1d0; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2a3140; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3a4356; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #2a3140; border-radius: 4px; min-width: 30px; }
QMenu { background: #161a22; border: 1px solid #2a3140; padding: 4px; border-radius: 8px; }
QMenu::item { padding: 6px 18px; border-radius: 5px; }
QMenu::item:selected { background: #243a66; }
QToolTip { background: #1c212b; color: #e6e8ee; border: 1px solid #2c3444; padding: 5px; }
QTextBrowser { background: #12151c; border: none; }
QFrame#scope { background: #12151c; border: 1px solid #242a36; border-radius: 10px; }
QFrame#scope[scope="item"] { background: #2a2110; border: 1px solid #6b4d16; }
QLabel#scopeTitle { font-size: 11pt; font-weight: 600; }
QLineEdit[custom="true"], QSpinBox[custom="true"], QComboBox[custom="true"] { border: 1px solid #d49a2a; }
QCheckBox[custom="true"] { color: #f0b43c; }
QLabel#previewFrame { background: #000; border: 1px solid #242a36; border-radius: 8px; color: #6f7a90; }
QLabel#tc { font-family: Consolas; font-size: 11pt; color: #e6e8ee; }
QLineEdit#tcEdit { font-family: Consolas; font-size: 11pt; padding: 4px 8px; }
QLabel#marks { font-family: Consolas; font-size: 9.5pt; color: #f0b43c; }
QPushButton#step { padding: 6px 10px; min-width: 44px; }
QPushButton#mark { background: #2a2110; border: 1px solid #6b4d16; color: #f0b43c; font-weight: 600; }
QPushButton#mark:hover { background: #3a2d14; }
QPushButton#add { background: #15321f; border: 1px solid #2a6a44; color: #7ee2a8; font-weight: 600; }
QPushButton#add:hover { background: #1b4229; }
QPushButton#add:disabled { color: #4d7a60; background: #10231a; border-color: #1f3d2c; }
QSlider::groove:horizontal { height: 6px; background: #1d222c; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #4c8dff; border-radius: 3px; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: #e6e8ee; }
QSlider::handle:horizontal:hover { background: #ffffff; }
"""

HELP_HTML = """
<h2>Getting the most out of a USB hard drive</h2>
<p>Thousands of small files are limited by <b>per-file overhead</b> (antivirus, filesystem
bookkeeping, write caching), not by raw MB/s. This program already streams frames straight
from memory to the drive while decoding. The Windows settings below make the biggest
difference, and each one is a one-off change.</p>
<h3>1. Turn on write caching for the drive</h3>
<p>Windows sets external drives to <i>Quick removal</i> by default, which switches write caching off,
so every small file waits for the disk heads. Go to <b>Device Manager → Disk drives → WD My Book …
→ Properties → Policies → Better performance</b>. Afterwards, always use <i>Safely Remove Hardware</i>
before unplugging or powering off the Duo.</p>
<h3>2. Exclude the frames folder from Microsoft Defender</h3>
<p>Defender scans every new JPEG as it is closed. <b>Windows Security → Virus &amp; threat protection
→ Manage settings → Exclusions → Add → Folder</b> and pick your frames root folder (for example
<code>D:\\Whodle\\Frames</code>).</p>
<h3>3. Turn off 8.3 short names on the drive</h3>
<p>Every frame of an episode starts with the same long name (<code>S2E04 School Reunion …</code>).
NTFS still generates a DOS-style <code>SCHOOL~1.JPE</code> alias for each file, and that gets slower
the more similar names a folder holds. From an <b>administrator</b> terminal (replace <code>D:</code> with the drive letter):<br>
<code>fsutil 8dot3name set D: 1</code><br>
This only affects files created from then on.</p>
<h3>4. Keep the drive out of the search index</h3>
<p>The program marks each output folder <i>not content-indexed</i> for you. If the whole drive is
indexed, untick <i>Allow files on this drive to have contents indexed</i> in the drive's Properties.</p>
<h3>Writer threads</h3>
<p>4 threads is a good default for a spinning drive. With the fixes above you can try 2–8 and use
<b>Test drive speed</b> to compare. The <b>RAM buffer</b> tile shows which side is holding things up:
a full buffer means the drive is the bottleneck, and an empty one means decoding is.</p>
"""


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(ACCENT))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    p.setBrush(QColor("#ffffff"))
    for i, alpha in enumerate((90, 160, 255)):
        c = QColor("#ffffff")
        c.setAlpha(alpha)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(12 + i * 7, 16 + i * 5, 26, 20), 4, 4)
    p.end()
    return QIcon(pm)


def card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    if title:
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return f, lay


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


class StatTile(QFrame):
    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        lay.addWidget(label(caption.upper(), "tileCap"))
        self.val = label("–", "tileVal")
        self.sub = label(" ", "tileSub")
        lay.addWidget(self.val)
        lay.addWidget(self.sub)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, value: str, sub: str = " ") -> None:
        self.val.setText(value)
        self.sub.setText(sub)


class QueueTable(QTableWidget):
    filesDropped = pyqtSignal(list)

    def __init__(self) -> None:
        super().__init__(0, 5)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            self.filesDropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
            e.acceptProposedAction()
        else:
            super().dropEvent(e)


class FrameGrabber(QObject):
    """Background ffmpeg frame grabs for the segment editor. Only the newest preview
    request is honoured (scrubbing collapses into one grab); filmstrip thumbnails are
    produced one by one in between."""
    frameReady = pyqtSignal(float, bytes)
    thumbReady = pyqtSignal(int, bytes)

    def __init__(self, video: str) -> None:
        super().__init__()
        self.video = video
        self._want: Optional[float] = None
        self._thumbs: deque = deque()
        self._cv = threading.Condition()
        self._stop = False
        threading.Thread(target=self._run, daemon=True, name="grab").start()

    def request(self, t: float) -> None:
        with self._cv:
            self._want = t
            self._cv.notify()

    def request_thumbs(self, times: list[float]) -> None:
        with self._cv:
            self._thumbs = deque(enumerate(times))
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stop and self._want is None and not self._thumbs:
                    self._cv.wait()
                if self._stop:
                    return
                if self._want is not None:
                    kind, t, idx = "frame", self._want, -1
                    self._want = None
                else:
                    idx, t = self._thumbs.popleft()
                    kind = "thumb"
            data = grab_frame(self.video, t, 960 if kind == "frame" else 192)
            if self._stop or not data:
                continue
            if kind == "frame":
                self.frameReady.emit(t, data)
            else:
                self.thumbReady.emit(idx, data)


class TimelineBar(QWidget):
    """Filmstrip of the whole video with the marked segments drawn on top. Click or
    drag to seek; clicking inside a segment also selects it in the list."""
    seekRequested = pyqtSignal(float)
    segmentClicked = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self.duration = 1.0
        self.pos = 0.0
        self.segs: list = []
        self.selected = -1
        self.mark_in: Optional[float] = None
        self.mark_out: Optional[float] = None
        self.thumbs: dict[int, QPixmap] = {}
        self.n_thumbs = 0
        self.setMinimumHeight(84)
        self.setMaximumHeight(84)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    # geometry helpers
    def _x(self, t: float) -> float:
        return max(0.0, min(1.0, t / max(self.duration, 1e-6))) * (self.width() - 1)

    def _t(self, x: float) -> float:
        return max(0.0, min(1.0, x / max(1, self.width() - 1))) * self.duration

    def set_thumb(self, idx: int, pix: QPixmap) -> None:
        self.thumbs[idx] = pix
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        strip = QRectF(0, 0, w, h - 14)                     # film area; bottom 14 px = ribbon
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0b0d11"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 8, 8)
        # filmstrip
        if self.n_thumbs:
            cell = w / self.n_thumbs
            for i in range(self.n_thumbs):
                r = QRectF(i * cell, 0, cell + 0.5, strip.height())
                pm = self.thumbs.get(i)
                if pm is not None and not pm.isNull():
                    scaled = pm.scaled(int(r.width()) + 1, int(r.height()),
                                       Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
                    src = QRectF((scaled.width() - r.width()) / 2, (scaled.height() - r.height()) / 2,
                                 r.width(), r.height())
                    p.setOpacity(0.55)
                    p.drawPixmap(r, scaled, src)
                    p.setOpacity(1.0)
                else:
                    p.setBrush(QColor("#12151c" if i % 2 else "#141821"))
                    p.drawRect(r)
        # dim everything, then un-dim the kept ranges
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawRect(strip)
        for i, (st, en) in enumerate(self.segs):
            x0, x1 = self._x(st), self._x(en if en is not None else self.duration)
            sel = i == self.selected
            fill = QColor(ACCENT)
            fill.setAlpha(70 if sel else 40)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRect(QRectF(x0, 0, max(1.0, x1 - x0), strip.height()))
            p.setBrush(QColor("#6a9fff" if sel else ACCENT))
            p.drawRoundedRect(QRectF(x0, h - 12, max(2.0, x1 - x0), 10), 3, 3)
            pen = QPen(QColor("#ffffff" if sel else "#9ec1ff"), 1.5 if sel else 1)
            p.setPen(pen)
            p.drawLine(QPointF(x0, 0), QPointF(x0, strip.height()))
            p.drawLine(QPointF(x1, 0), QPointF(x1, strip.height()))
            if x1 - x0 > 22:
                p.setPen(QColor("#ffffff"))
                f = p.font()
                f.setPointSizeF(7.5)
                f.setBold(True)
                p.setFont(f)
                p.drawText(QRectF(x0 + 4, 2, x1 - x0 - 8, 14), Qt.AlignmentFlag.AlignLeft, str(i + 1))
        # pending in/out marks
        for t, is_in in ((self.mark_in, True), (self.mark_out, False)):
            if t is None:
                continue
            x = self._x(t)
            amber = QColor("#f0b43c")
            p.setPen(QPen(amber, 2))
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(amber)
            tri = QPolygonF([QPointF(x, 10), QPointF(x + (8 if is_in else -8), 4), QPointF(x + (8 if is_in else -8), 16)])
            p.drawPolygon(tri)
        if self.mark_in is not None and self.mark_out is not None and self.mark_out > self.mark_in:
            amber = QColor("#f0b43c")
            amber.setAlpha(45)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(amber)
            p.drawRect(QRectF(self._x(self.mark_in), 0, self._x(self.mark_out) - self._x(self.mark_in), strip.height()))
        # playhead
        x = self._x(self.pos)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPolygon(QPolygonF([QPointF(x - 6, h), QPointF(x + 6, h), QPointF(x, h - 7)]))
        p.end()

    def _hit(self, x: float) -> int:
        t = self._t(x)
        for i, (st, en) in enumerate(self.segs):
            if st <= t <= (en if en is not None else self.duration):
                return i
        return -1

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            x = e.position().x()
            hit = self._hit(x)
            if hit >= 0:
                self.segmentClicked.emit(hit)
            self.seekRequested.emit(self._t(x))

    def mouseMoveEvent(self, e) -> None:
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.seekRequested.emit(self._t(e.position().x()))
        else:
            self.setToolTip(fmt_tc(self._t(e.position().x())))


class SegmentDialog(QDialog):
    """Mark the parts of one video to extract. Scrub with the slider / filmstrip or
    step frame by frame, press I and O for in/out points, Enter to add the segment."""
    N_THUMBS = 28

    def __init__(self, parent, job: Job, info: VideoInfo, decimation: int) -> None:
        super().__init__(parent)
        self.job, self.info, self.decimation = job, info, decimation
        self.dur = info.duration if info.duration > 0 else 1.0
        self.fps = info.avg_fps if info.avg_fps > 0 else (info.fps_num / info.fps_den if info.fps_den else 25.0)
        self.frame = 1.0 / self.fps
        self.segs: Segs = tidy_segments(job.segments() if job.seg_source() != "whole" else [], self.dur)
        self._orig = list(self.segs)
        self.t = 0.0
        self.mark_in: Optional[float] = None
        self.mark_out: Optional[float] = None
        self._pix: Optional[QPixmap] = None
        self._loading = False
        self.setWindowTitle(f"Segments – {job.base_name}")
        self.resize(1240, 760)
        self.setSizeGripEnabled(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build()
        self.grabber = FrameGrabber(job.video)
        self.grabber.frameReady.connect(self._frame_ready)
        self.grabber.thumbReady.connect(self._thumb_ready)
        self.timeline.n_thumbs = self.N_THUMBS
        self.grabber.request_thumbs([(i + 0.5) * self.dur / self.N_THUMBS for i in range(self.N_THUMBS)])
        self._refresh_list()
        self._seek(self.segs[0][0] if self.segs else 0.0)

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label(f"{Path(self.job.video).name}", "cardTitle"))
        top.addStretch(1)
        top.addWidget(label(f"{self.info.codec} {self.info.width}×{self.info.height} · "
                            f"{self.fps:.3f} fps · {fmt_duration(self.dur)}", "hint"))
        root.addLayout(top)
        root.addWidget(label("Only the marked segments are extracted; frame numbering runs on continuously "
                             "from one segment to the next. The In point is the first frame kept and "
                             "the Out point is the last frame kept.", "hint", wrap=True))

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # left: preview + transport
        left = QVBoxLayout()
        left.setSpacing(8)
        self.preview = QLabel("Loading frame…")
        self.preview.setObjectName("previewFrame")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(560, 315)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        left.addWidget(self.preview, 1)
        self.timeline = TimelineBar()
        self.timeline.duration = self.dur
        self.timeline.seekRequested.connect(lambda t: self._seek(t))
        self.timeline.segmentClicked.connect(self._select_row)
        left.addWidget(self.timeline)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, int(self.dur * 1000))
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.valueChanged.connect(self._slider_moved)
        left.addWidget(self.slider)

        trans = QHBoxLayout()
        trans.setSpacing(6)
        self.tc_edit = QLineEdit()
        self.tc_edit.setObjectName("tcEdit")
        self.tc_edit.setFixedWidth(130)
        self.tc_edit.setToolTip("Type a time (h:mm:ss.mmm or seconds) and press Enter to jump there")
        self.tc_edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)      # keys stay with the editor until clicked
        self.tc_edit.editingFinished.connect(self._tc_entered)
        trans.addWidget(self.tc_edit)
        trans.addWidget(label(f"/ {fmt_tc(self.dur)}", "hint"))
        self.frame_lbl = label("", "hint")
        trans.addWidget(self.frame_lbl)
        trans.addStretch(1)
        for text, tip, fr, sec in (("⏮", "Start of video (Home)", None, None),
                                   ("−10s", "Back 10 seconds (Ctrl+←)", 0, -10.0),
                                   ("−1s", "Back 1 second (Shift+←)", 0, -1.0),
                                   ("◂ 1f", "Back one frame (←)", -1, 0.0),
                                   ("1f ▸", "Forward one frame (→)", 1, 0.0),
                                   ("+1s", "Forward 1 second (Shift+→)", 0, 1.0),
                                   ("+10s", "Forward 10 seconds (Ctrl+→)", 0, 10.0),
                                   ("⏭", "End of video (End)", None, None)):
            b = QPushButton(text)
            b.setObjectName("step")
            b.setToolTip(tip)
            b.setAutoDefault(False)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if fr is None:
                b.clicked.connect((lambda: self._seek(0.0)) if text == "⏮" else (lambda: self._seek(self.dur)))
            else:
                b.clicked.connect(lambda _=False, f=fr, s=sec: self._step(f, s))
            trans.addWidget(b)
        left.addLayout(trans)

        marks = QHBoxLayout()
        marks.setSpacing(8)
        self.b_in = QPushButton("⟦  Set In   (I)")
        self.b_in.setObjectName("mark")
        self.b_in.clicked.connect(self._set_in)
        self.b_out = QPushButton("Set Out  (O)  ⟧")
        self.b_out.setObjectName("mark")
        self.b_out.clicked.connect(self._set_out)
        self.marks_lbl = label("", "marks")
        self.b_clear_marks = QPushButton("Clear marks")
        self.b_clear_marks.setObjectName("ghost")
        self.b_clear_marks.clicked.connect(self._clear_marks)
        self.b_addseg = QPushButton("＋  Add segment  (Enter)")
        self.b_addseg.setObjectName("add")
        self.b_addseg.clicked.connect(self._add_segment)
        for b in (self.b_in, self.b_out, self.b_clear_marks, self.b_addseg):
            b.setAutoDefault(False)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        marks.addWidget(self.b_in)
        marks.addWidget(self.b_out)
        marks.addWidget(self.marks_lbl, 1)
        marks.addWidget(self.b_clear_marks)
        marks.addWidget(self.b_addseg)
        left.addLayout(marks)
        body.addLayout(left, 1)

        # right: the list
        rcard, rl = card()
        rcard.setFixedWidth(360)
        rh = QHBoxLayout()
        self.list_title = label("Segments", "cardTitle")
        rh.addWidget(self.list_title)
        rh.addStretch(1)
        rl.addLayout(rh)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Start", "End", "Length"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._cell_edited)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.itemSelectionChanged.connect(self._list_selection)
        self.table.setToolTip("Click a row to jump to it. Double-click a time to edit it.")
        self.table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        rl.addWidget(self.table, 1)
        self.sum_lbl = label("", "hint", wrap=True)
        rl.addWidget(self.sum_lbl)
        rb = QGridLayout()
        rb.setSpacing(6)
        self.b_remove = QPushButton("Remove")
        self.b_remove.clicked.connect(self._remove_rows)
        self.b_clear = QPushButton("Clear all")
        self.b_clear.clicked.connect(self._clear_all)
        self.b_import = QPushButton("Import CSV…")
        self.b_import.setToolTip("Load segments from a LosslessCut CSV")
        self.b_import.clicked.connect(self._import_csv)
        self.b_export = QPushButton("Export CSV…")
        self.b_export.setToolTip("Save these segments as a LosslessCut-compatible CSV")
        self.b_export.clicked.connect(self._export_csv)
        for i, b in enumerate((self.b_remove, self.b_clear, self.b_import, self.b_export)):
            b.setAutoDefault(False)
            rb.addWidget(b, i // 2, i % 2)
        rl.addLayout(rb)
        body.addWidget(rcard)

        foot = QHBoxLayout()
        foot.addWidget(label("Keys:  ← →  frame · Shift ±1 s · Ctrl ±10 s ·  I / O  in / out ·  "
                             "Enter  add segment ·  Delete  remove selected", "hint", wrap=True), 1)
        bb = QDialogButtonBox()
        self.b_ok = bb.addButton("Save segments", QDialogButtonBox.ButtonRole.AcceptRole)
        self.b_ok.setObjectName("primary")
        self.b_cancel = bb.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        for b in (self.b_ok, self.b_cancel):
            b.setAutoDefault(False)
            b.setDefault(False)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        foot.addWidget(bb)
        root.addLayout(foot)

    # ── seeking & preview ──────────────────────────────────────────────
    def _snap(self, t: float) -> float:
        t = max(0.0, min(t, max(0.0, self.dur - self.frame)))
        return round(round(t * self.fps) / self.fps, 4)

    def _seek(self, t: float) -> None:
        self.t = self._snap(t)
        self._loading = True
        try:
            self.slider.setValue(int(round(self.t * 1000)))
            if not self.tc_edit.hasFocus():
                self.tc_edit.setText(fmt_tc(self.t))
        finally:
            self._loading = False
        self.frame_lbl.setText(f"frame {int(round(self.t * self.fps)):,}")
        self.timeline.pos = self.t
        self.timeline.update()
        self.grabber.request(self.t)

    def _slider_moved(self, v: int) -> None:
        if not self._loading:
            self._seek(v / 1000.0)

    def _step(self, frames: int, secs: float) -> None:
        self._seek(self.t + frames * self.frame + secs)

    def _tc_entered(self) -> None:
        t = parse_time(self.tc_edit.text())
        if t is None:
            self.tc_edit.setText(fmt_tc(self.t))
            return
        self._seek(t)
        self.tc_edit.setText(fmt_tc(self.t))
        self.tc_edit.clearFocus()
        self.setFocus()

    def _frame_ready(self, t: float, data: bytes) -> None:
        pm = QPixmap()
        if pm.loadFromData(bytes(data)):
            self._pix = pm
            self._fit_preview()

    def _thumb_ready(self, idx: int, data: bytes) -> None:
        pm = QPixmap()
        if pm.loadFromData(bytes(data)):
            self.timeline.set_thumb(idx, pm)

    def _fit_preview(self) -> None:
        if self._pix is None:
            return
        area = self.preview.contentsRect().size()
        self.preview.setPixmap(self._pix.scaled(area, Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit_preview()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.setFocus()                 # so I / O / arrows work straight away

    # ── in / out marks ──────────────────────────────────────────────────
    def _set_in(self) -> None:
        self.mark_in = self.t
        if self.mark_out is not None and self.mark_out < self.mark_in:
            self.mark_out = None
        self._marks_changed()

    def _set_out(self) -> None:
        self.mark_out = self.t
        if self.mark_in is not None and self.mark_in > self.mark_out:
            self.mark_in = None
        self._marks_changed()

    def _clear_marks(self) -> None:
        self.mark_in = self.mark_out = None
        self._marks_changed()

    def _marks_changed(self) -> None:
        self.timeline.mark_in, self.timeline.mark_out = self.mark_in, self.mark_out
        self.timeline.update()
        bits = []
        if self.mark_in is not None:
            bits.append(f"In {fmt_tc(self.mark_in)}")
        if self.mark_out is not None:
            bits.append(f"Out {fmt_tc(self.mark_out)}")
        if self.mark_in is not None and self.mark_out is not None:
            bits.append(f"({fmt_duration(self.mark_out + self.frame - self.mark_in)})")
        self.marks_lbl.setText("   ".join(bits))
        self.b_clear_marks.setVisible(bool(bits))
        self.b_addseg.setEnabled(bool(bits))
        self.b_addseg.setToolTip("Adds In → Out. With only an In point the segment runs to the end of "
                                 "the video; with only an Out point it starts at the beginning.")

    def _add_segment(self) -> None:
        if self.mark_in is None and self.mark_out is None:
            return
        st = self.mark_in if self.mark_in is not None else 0.0
        en = None if self.mark_out is None else self.mark_out + self.frame
        new = tidy_segments([(st, en)], self.dur)
        if not new:
            return
        self.segs = tidy_segments(self.segs + new, self.dur)
        self.mark_in = self.mark_out = None
        self._marks_changed()
        self._refresh_list(select=self.segs.index(new[0]) if new[0] in self.segs else -1)

    # ── list ────────────────────────────────────────────────────────────
    def _refresh_list(self, select: int = -1) -> None:
        self._loading = True
        try:
            self.table.setRowCount(len(self.segs))
            for r, (st, en) in enumerate(self.segs):
                end_t = en if en is not None else self.dur
                cells = (fmt_tc(st), fmt_tc(en) if en is not None else "end", fmt_duration(end_t - st))
                for c, text in enumerate(cells):
                    it = QTableWidgetItem(text)
                    if c == 2:
                        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        it.setForeground(QColor("#9aa3b5"))
                    self.table.setItem(r, c, it)
            if 0 <= select < len(self.segs):
                self.table.selectRow(select)
        finally:
            self._loading = False
        self.timeline.segs = list(self.segs)
        self.timeline.selected = self.table.currentRow() if self.table.selectedItems() else -1
        self.timeline.update()
        n = len(self.segs)
        self.list_title.setText(f"Segments ({n})" if n else "Segments")
        if n:
            kept = segments_total(self.segs, self.dur)
            frames = int(kept * self.fps / max(1, self.decimation) + 0.999)
            self.sum_lbl.setText(f"{fmt_duration(kept)} of {fmt_duration(self.dur)} kept "
                                 f"({100 * kept / self.dur:.0f}%) · ≈{frames:,} frames at keep 1/{self.decimation}")
        else:
            self.sum_lbl.setText("No segments yet: the whole video would be extracted. Mark an In and an "
                                 "Out point, then press Enter.")
        self.b_remove.setEnabled(n > 0)
        self.b_clear.setEnabled(n > 0)
        self.b_export.setEnabled(n > 0)

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectionModel().selectedRows()})

    def _select_row(self, r: int) -> None:
        if 0 <= r < self.table.rowCount():
            self.table.selectRow(r)

    def _list_selection(self) -> None:
        if self._loading:
            return
        rows = self._selected_rows()
        self.timeline.selected = rows[0] if len(rows) == 1 else -1
        self.timeline.update()

    def _cell_clicked(self, r: int, c: int) -> None:
        if not (0 <= r < len(self.segs)):
            return
        st, en = self.segs[r]
        if c == 1:
            self._seek((en if en is not None else self.dur) - self.frame)
        else:
            self._seek(st)

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() > 1:
            return
        r = item.row()
        if not (0 <= r < len(self.segs)):
            return
        st, en = self.segs[r]
        txt = item.text().strip().lower()
        if item.column() == 0:
            v = parse_time(txt)
            if v is not None:
                st = v
        else:
            en = None if txt in ("", "end", "eof", "–", "-") else parse_time(txt)
            if en is None and txt not in ("", "end", "eof", "–", "-"):
                en = self.segs[r][1]
        fixed = tidy_segments([(st, en)], self.dur)
        rest = self.segs[:r] + self.segs[r + 1:]
        self.segs = tidy_segments(rest + fixed, self.dur)
        self._refresh_list(select=self.segs.index(fixed[0]) if fixed and fixed[0] in self.segs else -1)

    def _remove_rows(self) -> None:
        rows = set(self._selected_rows())
        if not rows:
            return
        self.segs = [s for i, s in enumerate(self.segs) if i not in rows]
        self._refresh_list()

    def _clear_all(self) -> None:
        if self.segs and QMessageBox.question(self, "Clear segments", "Remove all segments for this video?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.segs = []
        self._refresh_list()

    def _import_csv(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "LosslessCut segments CSV", os.path.dirname(self.job.video),
                                           "CSV files (*.csv);;All files (*)")
        if not f:
            return
        try:
            segs = tidy_segments(load_segments(f), self.dur)
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Can't read the CSV:\n{ex}")
            return
        if not segs:
            QMessageBox.information(self, APP_NAME, "No segments were found in that file.")
            return
        if self.segs and QMessageBox.question(self, "Import CSV", f"Replace the current {len(self.segs)} "
                                              f"segment(s) with the {len(segs)} from the CSV?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.segs = segs
        self._refresh_list()

    def _export_csv(self) -> None:
        if not self.segs:
            return
        default = str(Path(self.job.video).with_name(Path(self.job.video).stem + "-segments.csv"))
        f, _ = QFileDialog.getSaveFileName(self, "Save segments as CSV", default, "CSV files (*.csv)")
        if not f:
            return
        try:
            save_segments(f, self.segs)
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Can't write the CSV:\n{ex}")

    # ── keys & lifecycle ────────────────────────────────────────────────
    def keyPressEvent(self, e) -> None:
        fw = self.focusWidget()
        typing = isinstance(fw, (QLineEdit, QAbstractSpinBox)) or (fw is not None and self.table.isAncestorOf(fw))
        in_table = fw is not None and (fw is self.table or self.table.isAncestorOf(fw))
        k, mods = e.key(), e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if in_table and k == Qt.Key.Key_Delete and self.table.state() != QAbstractItemView.State.EditingState:
            self._remove_rows()
            return
        if not typing:
            if k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                sign = -1 if k == Qt.Key.Key_Left else 1
                if ctrl:
                    self._step(0, 10.0 * sign)
                elif shift:
                    self._step(0, 1.0 * sign)
                else:
                    self._step(sign, 0.0)
                return
            if k == Qt.Key.Key_Home:
                self._seek(0.0)
                return
            if k == Qt.Key.Key_End:
                self._seek(self.dur)
                return
            if k == Qt.Key.Key_I:
                self._set_in()
                return
            if k == Qt.Key.Key_O:
                self._set_out()
                return
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Plus):
                self._add_segment()
                return
            if k == Qt.Key.Key_Delete:
                self._remove_rows()
                return
            if k == Qt.Key.Key_Escape and (self.mark_in is not None or self.mark_out is not None):
                self._clear_marks()
                return
        super().keyPressEvent(e)

    def reject(self) -> None:
        if self.segs != self._orig or self.mark_in is not None or self.mark_out is not None:
            r = QMessageBox.question(self, "Discard changes?",
                                     "The segments for this video have changed. Close without saving them?")
            if r != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def done(self, result: int) -> None:
        self.grabber.stop()
        super().done(result)


class HelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drive speed tips")
        self.resize(640, 620)
        lay = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(HELP_HTML)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class MainWindow(QMainWindow):
    COL_NAME, COL_SRC, COL_SEG, COL_OUT, COL_PROG = range(5)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.resize(1320, 840)
        self.setAcceptDrops(True)
        self.jobs: list[Job] = []
        self.engine: Optional[Engine] = None
        self.bench_thread: Optional[threading.Thread] = None
        self.bench_result: Optional[tuple] = None
        self._rate_samples: deque = deque(maxlen=40)
        self._updating = False
        self.qs = QSettings("FrameExtractor", "FrameExtractor")
        self._build()
        self._load_settings()
        self._refresh_table()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(14)

        # header
        head = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(make_icon().pixmap(38, 38))
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label(APP_NAME, "h1"))
        titles.addWidget(label("Frame-exact decimation · streamed straight to disk", "sub"))
        head.addLayout(titles)
        head.addStretch(1)
        self.banner = label("", "banner")
        self.banner.hide()
        head.addWidget(self.banner)
        head.addSpacing(8)
        self.btn_help = QPushButton("Drive tips")
        self.btn_help.setObjectName("ghost")
        self.btn_help.clicked.connect(lambda: HelpDialog(self).exec())
        head.addWidget(self.btn_help)
        self.btn_bench = QPushButton("Test drive speed")
        self.btn_bench.setObjectName("ghost")
        self.btn_bench.clicked.connect(self._bench)
        head.addWidget(self.btn_bench)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.hide()
        head.addWidget(self.btn_stop)
        self.btn_start = QPushButton("Start extraction")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start)
        head.addWidget(self.btn_start)
        outer.addLayout(head)

        # stats row
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.t_extract = StatTile("Extracted")
        self.t_written = StatTile("Written to disk")
        self.t_speed = StatTile("Disk speed")
        self.t_buffer = StatTile("RAM buffer")
        self.t_time = StatTile("Time")
        for t in (self.t_extract, self.t_written, self.t_speed, self.t_buffer, self.t_time):
            stats.addWidget(t)
        outer.addLayout(stats)

        # body: queue | settings
        body = QHBoxLayout()
        body.setSpacing(14)
        outer.addLayout(body, 1)

        qcard, ql = card()
        top = QHBoxLayout()
        top.addWidget(label("Queue", "cardTitle"))
        top.addStretch(1)
        self.count_lbl = label("", "hint")
        top.addWidget(self.count_lbl)
        ql.addLayout(top)

        self.table = QueueTable()
        self.table.setHorizontalHeaderLabels(["Name  ✎ (double-click)", "Source video",
                                              "Segments  ✂ (double-click)", "Saves to", "Progress"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(self.COL_SRC, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(self.COL_OUT, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(self.COL_SRC, 190)
        hh.setSectionResizeMode(self.COL_SEG, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_PROG, QHeaderView.ResizeMode.Fixed)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setColumnWidth(self.COL_NAME, 220)
        self.table.setColumnWidth(self.COL_PROG, 300)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.filesDropped.connect(self._files_dropped)
        self.table.cellDoubleClicked.connect(
            lambda _r, c: self._edit_segments() if c == self.COL_SEG else None)
        ql.addWidget(self.table, 1)

        self.empty_lbl = label("Drop episode videos here or click “Add videos”.\n\n"
                               "Then double-click a row's Segments cell (or press “Segments…”) to mark the "
                               "parts of each video to extract.\nLosslessCut CSVs can also be dropped onto a row.",
                               "empty")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty_lbl.setParent(self.table.viewport())

        btns = QHBoxLayout()
        self.b_add = QPushButton("＋  Add videos")
        self.b_add.clicked.connect(self._add_dialog)
        self.b_segs = QPushButton("✂  Segments…")
        self.b_segs.setToolTip("Mark the parts of the selected video to extract (with a frame preview). "
                               "Each video in the queue has its own segments.")
        self.b_segs.clicked.connect(self._edit_segments)
        self.b_csv = QPushButton("Attach CSV…")
        self.b_csv.setToolTip("Use segments from a LosslessCut CSV for the selected row")
        self.b_csv.clicked.connect(self._attach_csv)
        self.b_rename = QPushButton("Rename")
        self.b_rename.setToolTip("Rename the selected entry (or double-click the name / press F2)")
        self.b_rename.clicked.connect(self._rename)
        self.b_auto = QPushButton("Auto-name…")
        self.b_auto.setToolTip("Name entries like 'S1E06 Dalek' from filenames such as '6 - Dalek'")
        self.b_auto.clicked.connect(self._auto_name_dialog)
        self.b_nocsv = QPushButton("Whole video")
        self.b_nocsv.setToolTip("Drop the segments / CSV of the selected rows and extract the whole video")
        self.b_nocsv.clicked.connect(self._clear_csv)
        self.b_remove = QPushButton("Remove")
        self.b_remove.clicked.connect(self._remove_selected)
        self.b_clear = QPushButton("Clear finished")
        self.b_clear.clicked.connect(self._clear_finished)
        for b in (self.b_add, self.b_segs, self.b_csv, self.b_nocsv, self.b_rename, self.b_auto):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.b_remove)
        btns.addWidget(self.b_clear)
        ql.addLayout(btns)

        self.overall = QProgressBar()
        self.overall.setObjectName("overall")
        self.overall.setTextVisible(False)
        self.overall.setRange(0, 1000)
        ql.addWidget(self.overall)
        body.addWidget(qcard, 1)

        # settings
        scard, sl = card()
        scope = QFrame()
        scope.setObjectName("scope")
        scl = QVBoxLayout(scope)
        scl.setContentsMargins(12, 10, 12, 10)
        scl.setSpacing(4)
        self.scope_title = label("", "scopeTitle", wrap=True)
        self.scope_sub = label("", "hint", wrap=True)
        scl.addWidget(self.scope_title)
        scl.addWidget(self.scope_sub)
        sbtn = QHBoxLayout()
        self.b_reset = QPushButton("Reset to defaults")
        self.b_reset.clicked.connect(self._reset_overrides)
        self.b_edit_defaults = QPushButton("Edit defaults instead")
        self.b_edit_defaults.clicked.connect(lambda: self.table.clearSelection())
        sbtn.addWidget(self.b_reset)
        sbtn.addWidget(self.b_edit_defaults)
        sbtn.addStretch(1)
        scl.addLayout(sbtn)
        self.scope_frame = scope
        sl.addWidget(scope)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setContentsMargins(0, 0, 6, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        scroll.setWidget(inner)
        sl.addWidget(scroll, 1)
        scard.setFixedWidth(400)
        body.addWidget(scard)
        self.settings_panel = inner

        def section(t):
            form.addRow(label(t.upper(), "section"))

        def spin(lo, hi, val, suffix=""):
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            s.setSuffix(suffix)
            s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            s.setAlignment(Qt.AlignmentFlag.AlignLeft)
            return s

        section("Output")
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText(r"e.g. D:\Whodle\Frames")
        ob = QPushButton("…")
        ob.setFixedWidth(36)
        ob.clicked.connect(self._browse_out)
        orow = QHBoxLayout()
        orow.setSpacing(6)
        orow.addWidget(self.out_edit, 1)
        orow.addWidget(ob)
        form.addRow("Frames folder", orow)
        self.organise = QCheckBox(r"Organise into Series N\<episode>\ folders")
        form.addRow(self.organise)

        section("Decimation")
        self.dec_spin = spin(1, 1000, 12)
        form.addRow("Keep every Nth frame", self.dec_spin)
        self.method = QComboBox()
        for k, v in METHODS.items():
            self.method.addItem(v, k)
        form.addRow("Method", self.method)
        self.dec_hint = label("", "hint", wrap=True)
        form.addRow(self.dec_hint)

        section("Naming")
        self.start_spin = spin(0, 9_999_999, 1)
        form.addRow("Start number", self.start_spin)
        self.pad_spin = spin(1, 10, 5, " digits")
        form.addRow("Padding", self.pad_spin)
        self.ext_combo = QComboBox()
        self.ext_combo.addItems([".jpeg", ".jpg", ".png"])
        form.addRow("Format", self.ext_combo)
        self.q_spin = spin(1, 31, 2)
        form.addRow("JPEG quality", self.q_spin)
        form.addRow(label("JPEG quality uses ffmpeg's qscale: 2 is near-lossless and larger numbers "
                          "give smaller files.", "hint", wrap=True))
        self.preview = label("", "preview", wrap=True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.preview)

        section("Performance  (all items)")
        self.dec_combo = QComboBox()
        for k, v in DECODERS.items():
            self.dec_combo.addItem(v, k)
        form.addRow("Decoder", self.dec_combo)
        self.thr_spin = spin(1, 32, 4)
        form.addRow("Disk writer threads", self.thr_spin)
        self.buf_spin = spin(128, 16384, 1024, " MB")
        self.buf_spin.setSingleStep(256)
        form.addRow("RAM buffer", self.buf_spin)
        form.addRow(label("Frames go from ffmpeg into RAM and then straight to the drive, "
                          "so no SSD staging is needed. The buffer lets decoding run ahead "
                          "while the drive catches up.", "hint", wrap=True))

        # log
        lcard, ll = card()
        lh = QHBoxLayout()
        lh.addWidget(label("Log", "cardTitle"))
        lh.addStretch(1)
        self.log_toggle = QPushButton("Hide")
        self.log_toggle.setObjectName("ghost")
        self.log_toggle.clicked.connect(self._toggle_log)
        lh.addWidget(self.log_toggle)
        ll.addLayout(lh)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFixedHeight(150)
        ll.addWidget(self.log)
        outer.addWidget(lcard)

        self._del_sc = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.table)
        self._del_sc.activated.connect(self._remove_selected)

        # per-item fields: (widget, getter, setter, change-signal)
        self._fields = {
            "output_root": (self.out_edit, lambda: self.out_edit.text().strip(),
                            self.out_edit.setText, self.out_edit.textChanged),
            "organise": (self.organise, self.organise.isChecked, self.organise.setChecked,
                         self.organise.toggled),
            "decimation": (self.dec_spin, self.dec_spin.value, self.dec_spin.setValue,
                           self.dec_spin.valueChanged),
            "method": (self.method, self.method.currentData,
                       lambda v: self._set_combo(self.method, v), self.method.currentIndexChanged),
            "start_number": (self.start_spin, self.start_spin.value, self.start_spin.setValue,
                             self.start_spin.valueChanged),
            "padding": (self.pad_spin, self.pad_spin.value, self.pad_spin.setValue,
                        self.pad_spin.valueChanged),
            "ext": (self.ext_combo, self.ext_combo.currentText, self.ext_combo.setCurrentText,
                    self.ext_combo.currentTextChanged),
            "quality": (self.q_spin, self.q_spin.value, self.q_spin.setValue, self.q_spin.valueChanged),
        }
        for name, (_w, _g, _s, sig) in self._fields.items():
            sig.connect(lambda *_, f=name: self._field_changed(f))

    # ── settings persistence ─────────────────────────────────────────────
    def _load_settings(self) -> None:
        q = self.qs
        d = Settings()
        self.defaults = Settings(
            output_root=q.value("output_root", d.output_root, str),
            organise=q.value("organise", d.organise, bool),
            decimation=q.value("decimation", d.decimation, int),
            method=q.value("method", d.method, str),
            start_number=q.value("start_number", d.start_number, int),
            padding=q.value("padding", d.padding, int),
            ext=q.value("ext", d.ext, str),
            quality=q.value("quality", d.quality, int),
        )
        self._set_combo(self.dec_combo, q.value("decoder", "auto", str))
        self.cuts_store: dict[str, list] = {}
        try:
            raw = json.loads(q.value("cuts_store", "{}", str) or "{}")
            if isinstance(raw, dict):
                self.cuts_store = {k: v for k, v in raw.items() if isinstance(v, list)}
        except (ValueError, TypeError):
            pass
        self.thr_spin.setValue(q.value("writer_threads", 4, int))
        self.buf_spin.setValue(q.value("buffer_mb", 1024, int))
        geo = q.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._load_panel()

    def _save_settings(self) -> None:
        s = self._settings()
        for k, v in vars(s).items():
            self.qs.setValue(k, v)
        self.qs.setValue("geometry", self.saveGeometry())
        self._save_cuts_store()

    # segments marked in the program are remembered per video so they survive a restart
    CUTS_REMEMBERED = 400

    @staticmethod
    def _cuts_key(video: str) -> str:
        return os.path.normcase(os.path.abspath(video))

    def _remember_cuts(self, j: Job) -> None:
        key = self._cuts_key(j.video)
        self.cuts_store.pop(key, None)
        if j.cuts:
            self.cuts_store[key] = [list(c) for c in j.cuts]
            while len(self.cuts_store) > self.CUTS_REMEMBERED:
                self.cuts_store.pop(next(iter(self.cuts_store)))
        self._save_cuts_store()

    def _save_cuts_store(self) -> None:
        self.qs.setValue("cuts_store", json.dumps(self.cuts_store))

    @staticmethod
    def _set_combo(combo: QComboBox, key: str) -> None:
        i = combo.findData(key)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _settings(self) -> Settings:
        """Defaults for the run (per-item overrides are applied by the engine)."""
        return dataclasses.replace(self.defaults,
                                   decoder=self.dec_combo.currentData(),
                                   writer_threads=self.thr_spin.value(),
                                   buffer_mb=self.buf_spin.value())

    def _eff(self, j: Optional[Job]) -> Settings:
        return effective_settings(self.defaults, j) if j else self.defaults

    def _scope(self) -> list[Job]:
        return [self.jobs[r] for r in self._selected_rows() if r < len(self.jobs)]

    def _selection_changed(self) -> None:
        if not self._updating:
            self._load_panel()

    def _load_panel(self) -> None:
        """Show the settings of the selected item(s), or the defaults when nothing is selected."""
        scope = self._scope()
        src = self._eff(scope[0]) if scope else self.defaults
        self._loading_panel = True
        try:
            for name, (_w, _g, setter, _sig) in self._fields.items():
                setter(getattr(src, name))
        finally:
            self._loading_panel = False
        self._after_settings_change()

    def _field_changed(self, name: str) -> None:
        if getattr(self, "_loading_panel", False) or not hasattr(self, "defaults"):
            return
        value = self._fields[name][1]()
        scope = self._scope()
        if not scope:
            setattr(self.defaults, name, value)
        else:
            for j in scope:
                if value == getattr(self.defaults, name):
                    j.overrides.pop(name, None)
                else:
                    j.overrides[name] = value
        self._after_settings_change()

    def _reset_overrides(self) -> None:
        for j in self._scope():
            j.overrides.clear()
        self._load_panel()

    def _after_settings_change(self) -> None:
        scope = self._scope()
        src = self._eff(scope[0]) if scope else self.defaults
        n = src.decimation
        if n == 1:
            self.dec_hint.setText("Keeps every frame.")
        else:
            self.dec_hint.setText(f"Keeps frame 1, {n + 1}, {2 * n + 1} … of each segment, "
                                  f"i.e. 1 of every {n}. At 25 fps that's {25 / n:.3g} frames "
                                  f"per second of video.")
        self.q_spin.setEnabled(src.ext != ".png")
        # amber outline on fields that differ from the defaults
        for name, (w, _g, _s, _sig) in self._fields.items():
            custom = bool(scope) and any(name in j.overrides for j in scope)
            if bool(w.property("custom")) != custom:
                w.setProperty("custom", custom)
                w.style().unpolish(w)
                w.style().polish(w)
        # scope header
        if not scope:
            self.scope_title.setText("Default settings")
            self.scope_sub.setText("Used by every queued item that doesn't have its own settings. "
                                   "Select items in the queue to give them their own folder, "
                                   "naming or decimation.")
            state = "defaults"
        else:
            who = scope[0].base_name if len(scope) == 1 else f"{len(scope)} selected items"
            n_custom = len({k for j in scope for k in j.overrides})
            self.scope_title.setText(f"Settings for {who}")
            extra = (f" {n_custom} setting(s) differ from the defaults and are outlined in amber."
                     if n_custom else " Currently the same as the defaults.")
            if len(scope) > 1:
                extra += " Showing the first item's values; a change applies to all selected."
            self.scope_sub.setText("Changes apply only to the selection." + extra)
            state = "item"
        self.b_reset.setVisible(bool(scope))
        self.b_reset.setEnabled(any(j.overrides for j in scope))
        self.b_edit_defaults.setVisible(bool(scope))
        if self.scope_frame.property("scope") != state:
            self.scope_frame.setProperty("scope", state)
            self.scope_frame.style().unpolish(self.scope_frame)
            self.scope_frame.style().polish(self.scope_frame)
        if not self._running():
            for j in self.jobs:
                if j.info and j.state == "queued":
                    j.expected = estimate_frames(j.info, j.segments(), self._eff(j).decimation)
        self._update_out_column()
        self._update_rows()
        self._update_preview()

    def _update_out_column(self) -> None:
        for r, j in enumerate(self.jobs):
            it = self.table.item(r, self.COL_OUT)
            if it is None:
                continue
            e = self._eff(j)
            folder = str(episode_dir(e.output_root, j.base_name, e.organise)) if e.output_root \
                else "⚠ no frames folder set"
            text = ("⚙ " if j.overrides else "") + folder
            if it.text() != text:
                it.setText(text)
            tip = folder
            if j.overrides:
                tip += "\n\nOwn settings:\n" + "\n".join(f"  {k.replace('_', ' ')}: {v}"
                                                        for k, v in j.overrides.items())
            it.setToolTip(tip)
            it.setForeground(QColor("#f0b43c" if j.overrides else "#9aa3b5"))

    def closeEvent(self, e):
        if self._running():
            r = QMessageBox.question(self, APP_NAME, "An extraction is running. Stop it and quit?")
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.engine.cancel()
            self.engine.wait(10)
        self._save_settings()
        e.accept()

    # ── queue management ─────────────────────────────────────────────────
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self._files_dropped([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def _files_dropped(self, paths: list[str]) -> None:
        if self._running():
            return
        csvs = [p for p in paths if p.lower().endswith(".csv")]
        vids = []
        for p in paths:
            if os.path.isdir(p):
                vids += sorted(str(x) for x in Path(p).iterdir() if x.suffix.lower() in VIDEO_EXTS)
            elif Path(p).suffix.lower() in VIDEO_EXTS:
                vids.append(p)
        if vids:
            self._add_videos(vids)
        if csvs:
            rows = self._selected_rows()
            if not rows and len(self.jobs) == 1:
                rows = [0]
            if len(rows) == 1:
                j = self.jobs[rows[0]]
                j.csv_path, j.cuts = csvs[0], []
                self._remember_cuts(j)
                self._job_settings_changed(j)
            else:
                self._flash("Select one row first, then drop the CSV onto it", warn=True)

    def _rename(self) -> None:
        rows = self._selected_rows()
        if not rows and len(self.jobs) == 1:
            rows = [0]
        if len(rows) != 1:
            self._flash("Select one row to rename (or use Auto-name… for several)", warn=True)
            return
        item = self.table.item(rows[0], self.COL_NAME)
        self.table.setCurrentItem(item)
        self.table.editItem(item)

    def _auto_name_dialog(self) -> None:
        if not self.jobs:
            return
        rows = self._selected_rows() or list(range(len(self.jobs)))
        jobs = [self.jobs[r] for r in rows]
        dlg = QDialog(self)
        dlg.setWindowTitle("Auto-name episodes")
        dlg.resize(620, 460)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(f"Renames {len(jobs)} {'selected ' if self._selected_rows() else ''}"
                            f"entr{'y' if len(jobs) == 1 else 'ies'} as S‹series›E‹episode› ‹title›. "
                            "The episode number is taken from the start of the filename "
                            "(e.g. “6 - Dalek”). If a filename has no number, queue order is used, "
                            "counting from the first episode number below.", "hint", wrap=True))
        form = QFormLayout()
        series = QSpinBox()
        series.setRange(0, 999)
        series.setValue(self.qs.value("auto_series", 1, int))
        first = QSpinBox()
        first.setRange(0, 999)
        first.setValue(1)
        form.addRow("Series", series)
        form.addRow("First episode (if no number)", first)
        lay.addLayout(form)
        table = QTableWidget(len(jobs), 2)
        table.setHorizontalHeaderLabels(["From", "Becomes"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        lay.addWidget(table, 1)

        def sources():
            return [default_base_name(j.video) for j in jobs]

        def names():
            return [auto_name(src, series.value(), first.value() + i)
                    for i, src in enumerate(sources())]

        def refresh(*_):
            for i, (src, new) in enumerate(zip(sources(), names())):
                table.setItem(i, 0, QTableWidgetItem(src))
                it = QTableWidgetItem(new)
                it.setForeground(QColor("#9ec1ff"))
                table.setItem(i, 1, it)
        series.valueChanged.connect(refresh)
        first.valueChanged.connect(refresh)
        refresh()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Rename")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.qs.setValue("auto_series", series.value())
            for j, new in zip(jobs, names()):
                if new:
                    j.base_name = new
            self._refresh_table()

    def _browse_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose the frames folder",
                                             self.out_edit.text().strip() or "")
        if d:
            self.out_edit.setText(os.path.normpath(d))

    def _add_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add episode videos",
                                                self.qs.value("last_video_dir", "", str),
                                                f"Video files ({exts});;All files (*)")
        if files:
            self.qs.setValue("last_video_dir", os.path.dirname(files[0]))
            self._add_videos(files)

    def _add_videos(self, files: list[str]) -> None:
        have = {os.path.normcase(os.path.abspath(j.video)) for j in self.jobs}
        new = []
        for f in files:
            key = os.path.normcase(os.path.abspath(f))
            if key in have:
                continue
            have.add(key)
            j = Job(video=f, base_name=default_base_name(f), csv_path=find_csv_for(f))
            remembered = self.cuts_store.get(self._cuts_key(f))
            if remembered:
                j.cuts = tidy_segments([(c[0], c[1]) for c in remembered if isinstance(c, list) and len(c) == 2])
            new.append(j)
        self.jobs += new
        if new and not self.defaults.output_root:
            self.defaults.output_root = os.path.dirname(new[0].video)
            if not self._scope():
                self._load_panel()
        self._refresh_table()
        if new:
            threading.Thread(target=self._probe_jobs, args=(new,), daemon=True).start()

    def _probe_jobs(self, jobs: list[Job]) -> None:
        for j in jobs:
            try:
                j.info = probe_video(j.video)
                if j.state == "queued":
                    j.expected = estimate_frames(j.info, j.segments(), self._eff(j).decimation)
            except Exception as e:  # noqa: BLE001
                j.error = f"Can't read video: {e}"
        self._probed = True          # picked up by _tick to refresh the Segments column

    def _job_settings_changed(self, j: Job) -> None:
        if j.info and j.state == "queued":
            j.expected = estimate_frames(j.info, j.segments(), self._eff(j).decimation)
        self._refresh_table()

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectionModel().selectedRows()})

    def _attach_csv(self) -> None:
        rows = self._selected_rows()
        if len(rows) != 1:
            self._flash("Select exactly one row to attach a CSV to", warn=True)
            return
        j = self.jobs[rows[0]]
        f, _ = QFileDialog.getOpenFileName(self, "LosslessCut segments CSV", os.path.dirname(j.video),
                                           "CSV files (*.csv);;All files (*)")
        if f:
            j.csv_path, j.cuts = f, []
            self._remember_cuts(j)
            self._job_settings_changed(j)

    def _clear_csv(self) -> None:
        for r in self._selected_rows():
            j = self.jobs[r]
            j.csv_path, j.cuts = None, []
            self._remember_cuts(j)
            self._job_settings_changed(j)

    def _edit_segments(self) -> None:
        if self._running():
            return
        rows = self._selected_rows()
        if not rows and len(self.jobs) == 1:
            rows = [0]
        if len(rows) != 1:
            self._flash("Select one row to edit its segments", warn=True)
            return
        j = self.jobs[rows[0]]
        if not os.path.isfile(j.video):
            QMessageBox.warning(self, APP_NAME, f"Video not found:\n{j.video}")
            return
        if not find_tool("ffmpeg") or not find_tool("ffprobe"):
            QMessageBox.critical(self, APP_NAME, "ffmpeg and ffprobe weren't found. Put them on PATH "
                                                 "or in the same folder as this program.")
            return
        if j.info is None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                j.info = probe_video(j.video)
            except Exception as ex:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Can't read the video:\n{ex}")
                return
            finally:
                QApplication.restoreOverrideCursor()
        if j.info.duration <= 0:
            QMessageBox.warning(self, APP_NAME, "ffprobe reports no duration for this video, so it can't be "
                                                "scrubbed. A LosslessCut CSV can still be attached.")
            return
        dlg = SegmentDialog(self, j, j.info, self._eff(j).decimation)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            j.cuts = [tuple(s) for s in dlg.segs]
            if j.cuts:
                j.csv_path = None
            self._remember_cuts(j)
            self._job_settings_changed(j)
            n = len(j.cuts)
            self._flash(f"{j.base_name}: {n} segment(s) marked" if n else f"{j.base_name}: whole video", ok=True)

    def _remove_selected(self) -> None:
        if self._running():
            return
        for r in reversed(self._selected_rows()):
            del self.jobs[r]
        self._refresh_table()

    def _clear_finished(self) -> None:
        if self._running():
            return
        self.jobs = [j for j in self.jobs if j.state != "done"]
        self._refresh_table()

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if row not in self._selected_rows():
            self.table.selectRow(row)
        j = self.jobs[row]
        m = QMenu(self)
        e = self._eff(j)
        target = j.out_dir or str(episode_dir(e.output_root, j.base_name, e.organise))
        a_open = m.addAction("Open output folder")
        a_open.setEnabled(os.path.isdir(target))
        a_src = m.addAction("Show source video")
        m.addSeparator()
        a_segs = m.addAction("Edit segments…")
        a_csv = m.addAction("Attach CSV…")
        a_nocsv = m.addAction("Use whole video")
        a_requeue = m.addAction("Queue again")
        a_reset = m.addAction("Reset settings to defaults")
        a_reset.setEnabled(not self._running() and any(x.overrides for x in self._scope()))
        m.addSeparator()
        a_rm = m.addAction("Remove")
        for a in (a_segs, a_csv, a_nocsv, a_requeue, a_rm):
            a.setEnabled(not self._running())
        a_segs.setEnabled(not self._running() and len(self._selected_rows()) == 1)
        a_requeue.setEnabled(not self._running() and j.state != "queued")
        act = m.exec(self.table.viewport().mapToGlobal(pos))
        if act == a_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        elif act == a_src:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(j.video)))
        elif act == a_segs:
            self._edit_segments()
        elif act == a_csv:
            self._attach_csv()
        elif act == a_nocsv:
            self._clear_csv()
        elif act == a_requeue:
            for r in self._selected_rows():
                self.jobs[r].reset()
                self._job_settings_changed(self.jobs[r])
        elif act == a_reset:
            self._reset_overrides()
        elif act == a_rm:
            self._remove_selected()

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != self.COL_NAME:
            return
        j = self.jobs[item.row()]
        clean = sanitize_name(item.text())
        if clean:
            j.base_name = clean
        self._updating = True
        item.setText(j.base_name)
        self._updating = False
        self._after_settings_change()

    # ── table rendering ──────────────────────────────────────────────────
    def _refresh_table(self) -> None:
        self._updating = True
        sel = self._selected_rows()
        self.table.setRowCount(len(self.jobs))
        for r, j in enumerate(self.jobs):
            name = QTableWidgetItem(j.base_name)
            name.setToolTip("Double-click to rename. Frames are saved as “<name> 00001.jpeg”.")
            self.table.setItem(r, self.COL_NAME, name)
            src = QTableWidgetItem(Path(j.video).name)
            src.setToolTip(j.video)
            src.setFlags(src.flags() & ~Qt.ItemFlag.ItemIsEditable)
            src.setForeground(QColor("#9aa3b5"))
            self.table.setItem(r, self.COL_SRC, src)
            seg = QTableWidgetItem(self._seg_text(j))
            seg.setToolTip(self._seg_tip(j))
            seg.setForeground(QColor("#9ec1ff" if j.cuts else "#e6e8ee"))
            seg.setFlags(seg.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, self.COL_SEG, seg)
            out = QTableWidgetItem("")
            out.setFlags(out.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, self.COL_OUT, out)
            if not isinstance(self.table.cellWidget(r, self.COL_PROG), QProgressBar):
                pb = QProgressBar()
                pb.setRange(0, 1000)
                wrap = QWidget()
                wl = QVBoxLayout(wrap)
                wl.setContentsMargins(4, 0, 8, 0)
                wl.addWidget(pb)
                self.table.setCellWidget(r, self.COL_PROG, wrap)
        sm = self.table.selectionModel()
        sm.clearSelection()
        for r in sel:
            if r < len(self.jobs):
                sm.select(self.table.model().index(r, 0),
                          QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self._updating = False
        self.empty_lbl.setVisible(not self.jobs)
        self._position_empty()
        self._load_panel()

    def _position_empty(self) -> None:
        self.empty_lbl.setGeometry(self.table.viewport().rect())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._position_empty()

    @staticmethod
    def _seg_text(j: Job) -> str:
        if j.cuts:
            n = len(j.cuts)
            kept = segments_total(j.cuts, j.info.duration) if j.info and j.info.duration > 0 else None
            return f"✂ {n} segment{'s' if n != 1 else ''}" + (f" · {fmt_duration(kept)}" if kept is not None else "")
        if j.csv_path:
            try:
                n = len(load_segments(j.csv_path))
                return f"{n} from CSV" if n else "CSV empty → whole"
            except OSError:
                return "CSV missing!"
        return "Whole video"

    @staticmethod
    def _seg_tip(j: Job) -> str:
        segs = j.segments()
        if j.cuts:
            head = "Segments marked in the program (double-click to edit):"
        elif j.csv_path:
            head = f"Segments from {j.csv_path}:"
        else:
            return "The whole video is extracted. Double-click to mark segments."
        lines = [f"  {i}.  {fmt_tc(st)}  →  {fmt_tc(en) if en is not None else 'end'}"
                 for i, (st, en) in enumerate(segs[:30], 1)]
        if len(segs) > 30:
            lines.append(f"  … and {len(segs) - 30} more")
        return head + "\n" + "\n".join(lines)

    def _update_rows(self) -> None:
        for r, j in enumerate(self.jobs):
            w = self.table.cellWidget(r, self.COL_PROG)
            if w is None:
                continue
            pb = w.findChild(QProgressBar)
            if pb is None:
                continue
            frac, text, state = self._job_status(j)
            pb.setValue(int(frac * 1000))
            pb.setFormat(text)
            pb.setToolTip(j.error or text)
            if pb.property("state") != state:
                pb.setProperty("state", state)
                pb.style().unpolish(pb)
                pb.style().polish(pb)
        n_done = sum(j.state == "done" for j in self.jobs)
        frames = sum(j.expected for j in self.jobs if j.state in ("queued",) and j.expected)
        txt = f"{len(self.jobs)} video(s)"
        if n_done:
            txt += f" · {n_done} done"
        if frames:
            txt += f" · ≈{frames:,} frames to go"
        self.count_lbl.setText(txt)

    @staticmethod
    def _job_status(j: Job) -> tuple[float, str, str]:
        st = j.state
        exp = max(j.expected, 1)
        if st == "queued":
            if j.error:
                return 0, j.error, "error"
            return 0, (f"Queued · ≈{j.expected:,} frames" if j.expected else "Queued"), "queued"
        if st == "probing":
            return 0, "Reading video…", "run"
        if st == "extracting":
            sp = f" · {j.ff_speed}" if j.ff_speed and j.ff_speed != "N/A" else ""
            return min(j.written / exp, 1), f"{j.written:,} / ≈{j.expected:,}{sp}", "run"
        if st == "flushing":
            return j.written / max(j.extracted, 1), f"Writing to disk {j.written:,} / {j.extracted:,}", "flushing"
        if st == "done":
            return 1, f"✓  {j.written:,} frames · {fmt_duration(j.t_end - j.t_start)}", "done"
        if st == "cancelled":
            return min(j.written / exp, 1), f"Stopped · {j.written:,} written", "error"
        return 1, f"Error: {j.error}", "error"

    def _update_preview(self, *_):
        scope = self._scope()
        j = scope[0] if scope else (self.jobs[0] if self.jobs else None)
        e = self._eff(scope[0]) if scope else self.defaults
        base = j.base_name if j else "S2E04 School Reunion"
        root = e.output_root or r"D:\Whodle\Frames"
        folder = episode_dir(root, base, e.organise)
        name = frame_name(base, e.start_number, e.padding, e.ext)
        self.preview.setText(f"{folder}{os.sep}\n{name}")

    # ── run control ──────────────────────────────────────────────────────
    def _running(self) -> bool:
        return self.engine is not None and not self.engine.finished.is_set()

    def _start(self) -> None:
        s = self._settings()
        if not find_tool("ffmpeg") or not find_tool("ffprobe"):
            QMessageBox.critical(self, APP_NAME, "ffmpeg and ffprobe weren't found. Put them on PATH "
                                                 "or in the same folder as this program.")
            return
        todo = [j for j in self.jobs if j.state != "done"]
        missing = [j.base_name for j in todo if not self._eff(j).output_root]
        if missing:
            self._flash(f"No frames folder set for {missing[0]}"
                        + (f" and {len(missing) - 1} more" if len(missing) > 1 else ""), warn=True)
            return
        if not todo:
            self._flash("Nothing queued. Add videos, or right-click a row and choose Queue again",
                        warn=True)
            return
        for j in todo:
            j.reset()
            if not os.path.isfile(j.video):
                QMessageBox.warning(self, APP_NAME, f"Video not found:\n{j.video}")
                return
        # collisions (same output folder + first filename)
        clashes = []
        seen = set()
        for j in todo:
            e = self._eff(j)
            d = episode_dir(e.output_root, j.base_name, e.organise)
            first = d / frame_name(j.base_name, e.start_number, e.padding, e.ext)
            key = os.path.normcase(str(first))
            if key in seen:
                QMessageBox.warning(self, APP_NAME, f"Two queue entries would write the same files:\n{first}\n\n"
                                                    "Give them different base names.")
                return
            seen.add(key)
            if first.exists():
                clashes.append(j.base_name)
        if clashes:
            more = f"\n… and {len(clashes) - 8} more" if len(clashes) > 8 else ""
            r = QMessageBox.question(self, "Overwrite existing frames?",
                                     "Frames already exist for:\n\n• " + "\n• ".join(clashes[:8]) + more +
                                     "\n\nOverwrite them?")
            if r != QMessageBox.StandardButton.Yes:
                return
        for root in {self._eff(j).output_root for j in todo}:
            try:
                Path(root).mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                QMessageBox.critical(self, APP_NAME, f"Can't create the frames folder:\n{root}\n\n{ex}")
                return
        self._save_settings()
        self._rate_samples.clear()
        self.log.appendPlainText(f"── Run started {time.strftime('%Y-%m-%d %H:%M:%S')} · "
                                 f"{len(todo)} video(s) · {DECODERS[s.decoder]} · "
                                 f"{s.writer_threads} writer threads")
        self.engine = Engine(self.jobs, s)
        self.engine.start()
        self._set_running(True)
        self._flash("")

    def _stop(self) -> None:
        if self._running():
            self.engine.cancel()
            self.btn_stop.setEnabled(False)
            self.btn_stop.setText("Stopping…")

    def _set_running(self, on: bool) -> None:
        self.btn_start.setVisible(not on)
        self.btn_stop.setVisible(on)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText("Stop")
        self.btn_bench.setEnabled(not on)
        self.settings_panel.setEnabled(not on)
        for b in (self.b_add, self.b_segs, self.b_rename, self.b_auto, self.b_csv, self.b_nocsv,
                  self.b_remove, self.b_clear):
            b.setEnabled(not on)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers if on else
                                   QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)

    def _flash(self, text: str, warn: bool = False, ok: bool = False) -> None:
        if not text:
            self.banner.hide()
            return
        bg, fg = ("#3a2a12", "#ffcf7a") if warn else (("#15321f", "#7ee2a8") if ok else ("#1c2a45", "#a9c6ff"))
        self.banner.setStyleSheet(f"background:{bg}; color:{fg};")
        self.banner.setText(text)
        self.banner.show()
        if warn:
            QTimer.singleShot(6000, lambda: self.banner.text() == text and self.banner.hide())

    def _toggle_log(self) -> None:
        vis = not self.log.isVisible()
        self.log.setVisible(vis)
        self.log_toggle.setText("Hide" if vis else "Show")

    # ── periodic refresh ────────────────────────────────────────────────
    def _tick(self) -> None:
        if self.bench_result is not None:
            self._bench_done(*self.bench_result)
            self.bench_result = None
        if getattr(self, "_probed", False) and not self._running():
            self._probed = False
            for r, j in enumerate(self.jobs):
                it = self.table.item(r, self.COL_SEG)
                if it is not None and it.text() != self._seg_text(j):
                    it.setText(self._seg_text(j))
                    it.setToolTip(self._seg_tip(j))
            self._update_rows()
        e = self.engine
        if e is None:
            return
        while True:
            try:
                self.log.appendPlainText(e.log_q.get_nowait())
            except queue.Empty:
                break
        self._update_rows()
        self._update_stats()
        if e.finished.is_set() and self.btn_stop.isVisible():
            self._finished()

    def _update_stats(self) -> None:
        e = self.engine
        w = e.writer
        now = time.time()
        run_jobs = [j for j in self.jobs if j.t_start >= e.t_start - 1 or j.state in ("queued", "probing")]
        extracted = sum(j.extracted for j in run_jobs)
        expected = sum(max(j.expected, j.extracted) for j in run_jobs)
        files = w.files_written if w else 0
        byts = w.bytes_written if w else 0
        self._rate_samples.append((now, files, byts))
        t0, f0, b0 = self._rate_samples[0]
        dt = now - t0
        fps = (files - f0) / dt if dt > 0.5 else 0.0
        mbs = (byts - b0) / dt / 1e6 if dt > 0.5 else 0.0
        self.t_extract.set(f"{extracted:,}", f"of ≈{expected:,}" if expected else " ")
        self.t_written.set(f"{files:,}", f"{byts / 1e9:.2f} GB" if byts >= 1e8 else f"{byts / 1e6:.0f} MB")
        self.t_speed.set(f"{fps:,.0f} files/s", f"{mbs:.1f} MB/s")
        if w:
            fill = w.buffer_fill
            active = any(j.state == "extracting" for j in self.jobs)
            hint = ("Drive is the bottleneck" if fill > 0.8 else
                    "Decoding is the bottleneck" if fill < 0.15 and active else "Balanced")
            self.t_buffer.set(f"{fill * 100:.0f}%", hint if active or fill > 0.01 else "Draining")
        elapsed = now - e.t_start
        remaining = max(expected - files, 0)
        eta = remaining / fps if fps > 1 and not e.finished.is_set() else None
        self.t_time.set(fmt_duration(elapsed), f"ETA {fmt_duration(eta)}" if eta else " ")
        self.overall.setValue(int(1000 * files / expected) if expected else 0)

    def _finished(self) -> None:
        e = self.engine
        self._update_rows()
        self._update_stats()
        self._set_running(False)
        run = [j for j in self.jobs if j.t_start >= e.t_start - 1]
        done = sum(j.state == "done" for j in run)
        errs = sum(j.state == "error" for j in run)
        frames = sum(j.written for j in run)
        el = fmt_duration(time.time() - e.t_start)
        if e.cancel_ev.is_set():
            self._flash(f"Stopped · {frames:,} frames written", warn=True)
        elif errs:
            self._flash(f"{done} done, {errs} failed · see the log", warn=True)
        else:
            self._flash(f"All done · {frames:,} frames in {el}", ok=True)
        self.log.appendPlainText(f"── Finished: {done} done, {errs} failed, {frames:,} frames, {el}")
        self.overall.setValue(1000 if not errs and not e.cancel_ev.is_set() else self.overall.value())
        QApplication.alert(self)

    # ── drive benchmark ─────────────────────────────────────────────────
    def _bench(self) -> None:
        root = self.out_edit.text().strip()
        if not root or not os.path.isdir(root):
            self._flash("Choose an existing frames folder to test", warn=True)
            return
        r = QMessageBox.question(self, "Test drive speed",
                                 f"Write 1,000 frame-sized test files (~150 MB) into a temporary "
                                 f"folder in\n{root}\nusing {self.thr_spin.value()} writer threads, "
                                 f"time it, then delete them?")
        if r != QMessageBox.StandardButton.Yes:
            return
        self.btn_bench.setEnabled(False)
        self.btn_start.setEnabled(False)
        self._flash("Testing drive…")
        threads = self.thr_spin.value()

        def work():
            try:
                self.bench_result = (benchmark_drive(root, threads), threads, None)
            except Exception as ex:  # noqa: BLE001
                self.bench_result = (None, threads, str(ex))
        threading.Thread(target=work, daemon=True).start()

    def _bench_done(self, res, threads, err) -> None:
        self.btn_bench.setEnabled(True)
        self.btn_start.setEnabled(True)
        if err:
            self._flash("Drive test failed", warn=True)
            self.log.appendPlainText(f"Drive test failed: {err}")
            return
        fps, mbs = res
        msg = f"Drive test ({threads} threads): {fps:,.0f} files/s · {mbs:.0f} MB/s"
        self.log.appendPlainText(msg + f"  →  a 5,600-frame episode would take ≈{fmt_duration(5600 / fps)} to write")
        if fps < 150:
            msg += " · slow, see Drive tips"
            self._flash(msg, warn=True)
        else:
            self._flash(msg, ok=True)


def main() -> None:
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FrameExtractor.2")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica"])
    app.setFont(f)
    pal = QPalette()
    for role, col in ((QPalette.ColorRole.Window, "#0e1015"), (QPalette.ColorRole.Base, "#0f1218"),
                      (QPalette.ColorRole.AlternateBase, "#161a22"), (QPalette.ColorRole.Text, "#e6e8ee"),
                      (QPalette.ColorRole.WindowText, "#e6e8ee"), (QPalette.ColorRole.Button, "#1f2531"),
                      (QPalette.ColorRole.ButtonText, "#e6e8ee"), (QPalette.ColorRole.Highlight, ACCENT),
                      (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, "#1c212b"),
                      (QPalette.ColorRole.ToolTipText, "#e6e8ee"), (QPalette.ColorRole.PlaceholderText, "#5b6375")):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
