"""
Stream Manager — handles FFmpeg processes that ingest remote streams
and output HLS segments for browser playback.

Design notes
------------
* Each stream gets its own sub-directory under ``streams/``.
* FFmpeg is spawned as a subprocess; we keep a reference so we can
  stop / restart it.
* The manager stores stream metadata in a plain JSON file so streams
  can be restored after a full server restart.
* A ``process_frame`` callback hook is reserved for future deep-learning
  integration.  When set, FFmpeg will decode to raw frames, the callback
  processes each frame, and re-encoded output is pushed to HLS.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import database
from .stream_handlers import StreamHandlerRegistry

logger = logging.getLogger(__name__)


# Browser-like User-Agent needed for YouTube URLs
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

_YOUTUBE_RE = re.compile(r"youtube\.com/|youtu\.be/")
_PLATFORM_RE = re.compile(r"twitch\.tv/|youtube\.com/|youtu\.be/")


def _detect_youtube_vod(url: str) -> bool:
    """Return *True* if *url* is a YouTube VOD (not a live stream)."""
    if not _YOUTUBE_RE.search(url):
        return False
    try:
        # Try without cookies first
        for extra_args in [[], ["--cookies-from-browser", "chrome"]]:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--js-runtimes",
                    "node",
                    *extra_args,
                    "--print",
                    "is_live",
                    "--no-download",
                    "--no-warnings",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                val = result.stdout.strip().lower()
                return val not in ("true",)
            if "Sign in to confirm" not in result.stderr:
                break  # Not a bot-detection issue
    except Exception:
        pass
    # Default to True (most YouTube URLs are VODs)
    return True


def resolve_stream_url(url: str) -> str:
    """
    Resolve platform URLs (Twitch, YouTube, etc.) to direct stream URLs.

    * YouTube → yt-dlp  (returns URL that works with correct User-Agent)
    * Twitch  → streamlink
    * Other   → return as-is
    """
    if not _PLATFORM_RE.search(url):
        return url

    # ---------- YouTube → yt-dlp ----------
    if _YOUTUBE_RE.search(url):
        for extra_args in [[], ["--cookies-from-browser", "chrome"]]:
            try:
                result = subprocess.run(
                    [
                        "yt-dlp",
                        "--js-runtimes",
                        "node",
                        *extra_args,
                        "-f",
                        "best",
                        "--print",
                        "urls",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode == 0:
                    resolved = result.stdout.strip().split("\n")[0]
                    if resolved.startswith("http"):
                        logger.info("Resolved (yt-dlp) %s → %s", url, resolved[:120])
                        return resolved
                if "Sign in to confirm" in result.stderr:
                    logger.info("Bot detection resolving URL, trying with cookies")
                    continue
                logger.warning(
                    "yt-dlp failed (rc=%d) for %s: %s",
                    result.returncode,
                    url,
                    result.stderr.strip()[:200],
                )
                break
            except subprocess.TimeoutExpired:
                logger.warning("yt-dlp timed out for %s", url)
                break
            except FileNotFoundError:
                logger.warning("yt-dlp not installed, trying streamlink for %s", url)
                break

    # ---------- Twitch / fallback → streamlink ----------
    try:
        result = subprocess.run(
            ["streamlink", "--stream-url", url, "best"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            resolved = result.stdout.strip()
            if resolved.startswith("http"):
                logger.info("Resolved (streamlink) %s → %s", url, resolved[:120])
                return resolved
        else:
            logger.warning(
                "streamlink failed (rc=%d) for %s: %s",
                result.returncode,
                url,
                result.stderr.strip()[:200],
            )
    except subprocess.TimeoutExpired:
        logger.warning("streamlink timed out for %s", url)
    except FileNotFoundError:
        logger.error("streamlink is not installed")

    return url


STREAMS_DIR = Path(__file__).resolve().parent.parent / "streams"
STATE_FILE = STREAMS_DIR / "_state.json"


@dataclass
class StreamInfo:
    id: str
    name: str
    source_url: str
    created_at: float = field(default_factory=time.time)
    status: str = "starting"  # starting | running | stopped | error | restarting
    error_message: str = ""
    is_platform_url: bool = False  # Twitch/YouTube etc. — don't auto-restore
    is_vod: bool = False  # YouTube VOD — download & loop locally


class ManagedStream:
    """Wraps a single FFmpeg process + its metadata."""

    def __init__(self, info: StreamInfo):
        self.info = info
        self.process: subprocess.Popen | None = None
        self._feeder: subprocess.Popen | None = None  # streamlink / yt-dlp
        self._stderr_path: Path | None = None
        self._stderr_fh = None
        self._video_path: Path | None = None  # cached VOD download
        self._handler = None  # StreamHandler instance, set by background task
        self.hls_dir = STREAMS_DIR / info.id
        self.hls_dir.mkdir(parents=True, exist_ok=True)

        self._stopping = False
        self._generation = 0  # bumped on every start(); old threads check this
        self._restart_count = 0
        self._restart_lock = threading.Lock()
        self._start_time = 0.0  # when current FFmpeg was spawned

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    @property
    def playlist_path(self) -> str:
        return str(self.hls_dir / "stream.m3u8")

    @property
    def playlist_url(self) -> str:
        return f"/streams/{self.info.id}/stream.m3u8"

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------
    def start(self) -> None:
        """(Re-)start the FFmpeg process for this stream."""
        # If already running, do nothing
        if self.process and self.process.poll() is None:
            logger.warning("Stream %s already running — skipping start()", self.info.id)
            return

        self._stopping = False

        # Bump generation so any old monitor / health-check threads exit
        self._generation += 1
        gen = self._generation

        # ---- Clean stale HLS files so health-check doesn't see old mtimes ----
        for f in self.hls_dir.glob("*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
        for f in self.hls_dir.glob("*.m3u8"):
            try:
                f.unlink()
            except OSError:
                pass
        for f in self.hls_dir.glob("*.part"):
            try:
                f.unlink()
            except OSError:
                pass

        try:
            if self.info.is_vod:
                self._start_vod(gen)
            elif self.info.is_platform_url:
                # Use piped mode only when the handler provides a feeder command
                feeder_cmd = (
                    self._handler.get_feeder_command(self.info.source_url) if self._handler else []
                )
                if feeder_cmd:
                    self._start_piped(gen)
                else:
                    self._start_direct(gen)
            else:
                self._start_direct(gen)
        except Exception as exc:
            self.info.status = "error"
            self.info.error_message = str(exc)
            logger.exception("Failed to start stream %s", self.info.id)
            return

        self.info.status = "running"
        self.info.error_message = ""
        self._start_time = time.time()

        # ---- Background threads ----
        threading.Thread(
            target=self._monitor,
            args=(gen,),
            daemon=True,
        ).start()

        if self.info.is_platform_url or self.info.is_vod:
            threading.Thread(
                target=self._health_check,
                args=(gen,),
                daemon=True,
            ).start()
            if not self.info.is_vod:
                threading.Thread(
                    target=self._periodic_token_refresh,
                    args=(gen,),
                    daemon=True,
                ).start()

    def _start_piped(self, gen: int) -> None:
        """Start feeder (streamlink/yt-dlp) piped into FFmpeg using handler."""
        if not self._handler:
            # Fallback to old logic if handler not set (e.g., restored from state)
            from .stream_handlers import StreamHandlerRegistry

            registry = StreamHandlerRegistry()
            self._handler = registry.get_handler(self.info.source_url)
            if not self._handler:
                raise RuntimeError("No handler found for URL")

        feeder_cmd = self._handler.get_feeder_command(self.info.source_url)

        logger.info(
            "Starting feeder for stream %s (gen %d): %s",
            self.info.id,
            gen,
            " ".join(feeder_cmd[:4]),
        )

        self._feeder = subprocess.Popen(
            feeder_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        ffmpeg_cmd = self._build_ffmpeg_cmd_generic(["-i", "pipe:0"])
        logger.info("Starting FFmpeg (piped) for stream %s (gen %d)", self.info.id, gen)

        self._stderr_path = self.hls_dir / "ffmpeg_stderr.log"
        self._stderr_fh = open(self._stderr_path, "w")
        self.process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=self._feeder.stdout,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_fh,
        )
        # Allow feeder to receive SIGPIPE if FFmpeg exits first
        if self._feeder.stdout:
            self._feeder.stdout.close()

    def _start_direct(self, gen: int) -> None:
        """Start FFmpeg directly with the URL (non-platform streams)."""
        if not self._handler:
            # Fallback to old logic if handler not set (e.g., restored from state)
            from .stream_handlers import StreamHandlerRegistry

            registry = StreamHandlerRegistry()
            self._handler = registry.get_handler(self.info.source_url)
            if not self._handler:
                raise RuntimeError("No handler found for URL")

        # Get input args from handler (returns tuple of (flags, input_source))
        input_flags, input_source = self._handler.get_ffmpeg_input_args(self.info.source_url)
        input_args = [*input_flags, "-i", input_source]
        cmd = self._build_ffmpeg_cmd_generic(input_args)

        logger.info("Starting FFmpeg (direct) for stream %s (gen %d)", self.info.id, gen)

        self._stderr_path = self.hls_dir / "ffmpeg_stderr.log"
        self._stderr_fh = open(self._stderr_path, "w")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_fh,
        )

    def _start_vod(self, gen: int) -> None:
        """Download YouTube video (once) and start FFmpeg in loop mode."""
        url = self.info.source_url

        # Re-use an earlier download if present
        existing = [
            f
            for f in self.hls_dir.glob("source_video.*")
            if f.suffix not in (".ts", ".m3u8", ".log", ".part")
        ]
        if existing:
            video_path = existing[0]
            logger.info("Reusing cached video %s for stream %s", video_path.name, self.info.id)
        else:
            self.info.status = "downloading"
            logger.info("Downloading video for stream %s (gen %d)…", self.info.id, gen)

            # Try multiple strategies for YouTube downloads
            strategies = [
                (["--js-runtimes", "node", "--no-warnings", "-f", "best"], "without cookies"),
                (
                    [
                        "--js-runtimes",
                        "node",
                        "--cookies-from-browser",
                        "chrome",
                        "--no-warnings",
                        "-f",
                        "best",
                    ],
                    "with Chrome cookies",
                ),
            ]

            result = None
            error_messages = []

            for extra_args, description in strategies:
                logger.info("Attempting download %s for stream %s", description, self.info.id)
                cmd = [
                    "yt-dlp",
                    *extra_args,
                    "-o",
                    str(self.hls_dir / "source_video.%(ext)s"),
                    url,
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )

                if result.returncode == 0:
                    logger.info("Download successful %s", description)
                    break
                else:
                    error_msg = result.stderr.strip()[:200]
                    error_messages.append(f"{description}: {error_msg}")
                    logger.warning("Download failed %s: %s", description, error_msg)
                    # Only retry with cookies if it's a bot detection issue
                    if "Sign in to confirm" not in result.stderr:
                        break

            if result and result.returncode != 0:
                combined_error = " | ".join(error_messages)
                raise RuntimeError(
                    f"yt-dlp download failed after trying all strategies. "
                    f"Please ensure you're logged in to YouTube in your browser. Errors: {combined_error[:400]}"
                )

            existing = [
                f
                for f in self.hls_dir.glob("source_video.*")
                if f.suffix not in (".ts", ".m3u8", ".log", ".part")
            ]
            if not existing:
                raise RuntimeError("yt-dlp produced no output file")
            video_path = existing[0]
            logger.info(
                "Downloaded %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1_048_576
            )

        self._video_path = video_path

        ffmpeg_cmd = self._build_ffmpeg_cmd_vod(str(video_path))
        logger.info("Starting FFmpeg (VOD loop) for stream %s (gen %d)", self.info.id, gen)

        self._stderr_path = self.hls_dir / "ffmpeg_stderr.log"
        self._stderr_fh = open(self._stderr_path, "w")
        self.process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_fh,
        )

    def stop(self) -> None:
        self._stopping = True
        self._generation += 1  # invalidate all background threads
        # Kill feeder first
        if self._feeder and self._feeder.poll() is None:
            try:
                self._feeder.kill()
                self._feeder.wait(timeout=3)
            except Exception:
                pass
        # Then FFmpeg
        if self.process and self.process.poll() is None:
            logger.info("Stopping stream %s (pid %d)", self.info.id, self.process.pid)
            try:
                self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception:
                self.process.kill()
        # Close stderr log file
        if self._stderr_fh:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stderr_fh = None
        self.info.status = "stopped"

    def cleanup(self) -> None:
        """Stop process and remove HLS files."""
        self.stop()
        if self.hls_dir.exists():
            shutil.rmtree(self.hls_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_ffmpeg_cmd_generic(self, input_args: list[str]) -> list[str]:
        """
        FFmpeg command when reading from generic input (URL, pipe, etc.).
        input_args should be complete list like ["-i", "pipe:0"] or ["-reconnect", "1", "-i", "url"].
        """
        out = str(self.hls_dir / "stream.m3u8")

        return [
            "ffmpeg",
            *input_args,
            # Video
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "30",
            "-sc_threshold",
            "0",
            # Audio
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            # HLS settings
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "10",
            "-hls_flags",
            "delete_segments+append_list",
            "-hls_segment_filename",
            str(self.hls_dir / "seg_%03d.ts"),
            out,
        ]

    def _build_ffmpeg_cmd_vod(self, video_path: str) -> list[str]:
        """FFmpeg command for looping a local VOD file at real-time speed."""
        out = str(self.hls_dir / "stream.m3u8")
        return [
            "ffmpeg",
            "-re",  # read at native frame rate
            "-stream_loop",
            "-1",  # loop forever
            "-i",
            video_path,
            # Video
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "30",
            "-sc_threshold",
            "0",
            # Audio
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            # HLS settings — standard live window
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_list_size",
            "10",
            "-hls_flags",
            "delete_segments+append_list",
            "-hls_segment_filename",
            str(self.hls_dir / "seg_%06d.ts"),
            out,
        ]

    # ---------- monitor thread ----------
    def _monitor(self, gen: int) -> None:
        """Wait for FFmpeg to exit; auto-restart platform streams."""
        if not self.process:
            return

        ret = self.process.wait()

        # Also kill feeder if it's still alive
        if self._feeder and self._feeder.poll() is None:
            try:
                self._feeder.kill()
            except OSError:
                pass

        # Stale thread or intentional stop → do nothing
        if gen != self._generation or self._stopping:
            return

        # Read last 500 chars of stderr for diagnostics
        stderr_tail = ""
        try:
            # Close the stderr file handle first
            if self._stderr_fh:
                self._stderr_fh.close()
                self._stderr_fh = None
            if self._stderr_path and self._stderr_path.exists():
                stderr_tail = self._stderr_path.read_text(errors="replace")[-500:]
        except Exception:
            pass

        if ret == 0:
            logger.info("Stream %s FFmpeg exited cleanly (gen %d)", self.info.id, gen)
            # For platform streams, feeder can drop — auto-restart
            if self.info.is_platform_url:
                self._try_restart(gen)
            else:
                self.info.status = "stopped"
            return

        logger.warning(
            "Stream %s FFmpeg exited code %d (gen %d). stderr: %s",
            self.info.id,
            ret,
            gen,
            stderr_tail.replace("\n", " ")[:300],
        )

        # Auto-restart platform streams
        if self.info.is_platform_url:
            self._try_restart(gen)
        else:
            self.info.status = "error"
            self.info.error_message = f"FFmpeg exited with code {ret}"

    # ---------- health-check thread ----------
    def _health_check(self, gen: int) -> None:
        """Kill FFmpeg if it stops producing segments (token expired, network lost)."""
        INITIAL_GRACE = 45  # seconds before first check
        CHECK_INTERVAL = 15  # seconds between checks
        STUCK_THRESHOLD = 90  # seconds with no new .ts → consider stuck

        # Wait for initial grace period (1-s ticks for quick exit)
        for _ in range(INITIAL_GRACE):
            time.sleep(1)
            if gen != self._generation or self._stopping:
                return

        while gen == self._generation and not self._stopping:
            proc = self.process
            if not proc or proc.poll() is not None:
                return  # process already dead — monitor thread handles it

            ts_files = list(self.hls_dir.glob("*.ts"))
            if ts_files:
                try:
                    newest_mtime = max(f.stat().st_mtime for f in ts_files)
                except (OSError, ValueError):
                    newest_mtime = time.time()

                age = time.time() - newest_mtime
                if age > STUCK_THRESHOLD:
                    logger.warning(
                        "Stream %s stuck — no new segments for %.0fs (gen %d), killing FFmpeg",
                        self.info.id,
                        age,
                        gen,
                    )
                    feeder = self._feeder
                    if feeder:
                        try:
                            feeder.kill()
                        except OSError:
                            pass
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return  # monitor thread picks up the exit and restarts
            else:
                # No segments at all yet — check if we've waited too long
                if time.time() - self._start_time > INITIAL_GRACE + 30:
                    logger.warning(
                        "Stream %s produced no segments after %.0fs (gen %d), killing FFmpeg",
                        self.info.id,
                        time.time() - self._start_time,
                        gen,
                    )
                    feeder = self._feeder
                    if feeder:
                        try:
                            feeder.kill()
                        except OSError:
                            pass
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return

            # Sleep in 1-s ticks so we can bail quickly on generation change
            for _ in range(CHECK_INTERVAL):
                time.sleep(1)
                if gen != self._generation or self._stopping:
                    return

    # ---------- periodic token refresh thread ----------
    def _periodic_token_refresh(self, gen: int) -> None:
        """Proactively restart with a fresh token before it expires."""
        REFRESH_INTERVAL = 50 * 60  # 50 minutes

        # Sleep in 1-s ticks for responsiveness
        for _ in range(REFRESH_INTERVAL):
            time.sleep(1)
            if gen != self._generation or self._stopping:
                return

        if gen != self._generation or self._stopping:
            return

        logger.info("Stream %s: proactive token refresh (gen %d)", self.info.id, gen)

        feeder = self._feeder
        if feeder:
            try:
                feeder.kill()
            except OSError:
                pass
        proc = self.process
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        # Monitor thread will see the exit and call _try_restart automatically

    # ---------- shared restart logic ----------
    def _try_restart(self, gen: int) -> None:
        """Attempt to restart a failed platform stream with back-off."""
        with self._restart_lock:
            # Another thread already restarted (generation moved on)
            if gen != self._generation or self._stopping:
                return

            self._restart_count += 1
            attempt = self._restart_count
            self.info.status = "restarting"

        # Exponential back-off: 3s, 6s, 12s … capped at 30s
        delay = min(3 * (2 ** (attempt - 1)), 30)
        logger.info(
            "Stream %s: will restart in %ds (attempt %d, gen %d)",
            self.info.id,
            delay,
            attempt,
            gen,
        )

        for _ in range(delay):
            time.sleep(1)
            if self._stopping:
                return

        if self._stopping:
            return

        logger.info("Stream %s: restarting now (attempt %d)", self.info.id, attempt)
        self.start()

        # If start succeeded and first segments appear, reset counter after a while
        threading.Thread(
            target=self._confirm_recovery,
            args=(self._generation,),
            daemon=True,
        ).start()

    def _confirm_recovery(self, gen: int) -> None:
        """After a restart, wait to see segments appear, then reset restart counter."""
        for _ in range(60):
            time.sleep(1)
            if gen != self._generation or self._stopping:
                return

        ts_files = list(self.hls_dir.glob("*.ts"))
        if ts_files and gen == self._generation:
            logger.info(
                "Stream %s: confirmed recovery after %d attempts — resetting counter",
                self.info.id,
                self._restart_count,
            )
            self._restart_count = 0


class StreamManager:
    """Manages all active streams per user."""

    def __init__(self) -> None:
        STREAMS_DIR.mkdir(parents=True, exist_ok=True)
        # Key: (user_id, stream_id) -> ManagedStream
        self._streams: dict[tuple[int, str], ManagedStream] = {}
        self._lock = threading.Lock()
        self._handler_registry = StreamHandlerRegistry()

    async def restore_streams(self) -> None:
        """Restore streams from database and restart them."""
        all_records = database.get_all_streams()
        if not all_records:
            logger.info("No streams to restore from database")
            return

        logger.info("Restoring %d stream(s) from database", len(all_records))

        for user_id, rec in all_records:
            try:
                info = StreamInfo(
                    id=rec.stream_id,
                    name=rec.name,
                    source_url=rec.source_url,
                    created_at=rec.created_at,
                    status="initializing",
                    is_platform_url=rec.is_platform_url,
                    is_vod=rec.is_vod,
                )
                managed = ManagedStream(info)
                with self._lock:
                    self._streams[(user_id, rec.stream_id)] = managed

                # Launch background initialization (same path as add_stream_async)
                asyncio.create_task(self._initialize_stream_background(user_id, managed, rec.name))
                logger.info(
                    "Queued restore for stream %s (%s) for user %d",
                    rec.stream_id,
                    rec.name,
                    user_id,
                )
            except Exception as e:
                logger.exception(
                    "Failed to restore stream %s for user %d: %s",
                    rec.stream_id,
                    user_id,
                    e,
                )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def add_stream_async(
        self, user_id: int, source_url: str, name: str | None = None
    ) -> StreamInfo:
        """
        Add a stream asynchronously. Returns immediately with 'initializing' status,
        then spawns background task to extract metadata and start the stream.
        This prevents blocking the API endpoint during metadata extraction or VOD downloads.
        """
        stream_id = uuid.uuid4().hex[:12]

        # Create stream with initializing status and placeholder name
        display_name = name if name else f"Stream {stream_id[:6]}"

        info = StreamInfo(
            id=stream_id,
            name=display_name,
            source_url=source_url,
            status="initializing",
            is_platform_url=False,  # Will be updated by background task
            is_vod=False,  # Will be updated by background task
        )

        managed = ManagedStream(info)
        with self._lock:
            self._streams[(user_id, stream_id)] = managed

        # Save to database
        database.save_stream(
            user_id=user_id,
            stream_id=stream_id,
            name=display_name,
            source_url=source_url,
            status="initializing",
        )

        # Start background task for metadata extraction and stream initialization
        asyncio.create_task(self._initialize_stream_background(user_id, managed, name))

        return info

    async def _initialize_stream_background(
        self, user_id: int, managed: ManagedStream, user_name: str | None
    ) -> None:
        """
        Background task that extracts metadata, derives name if needed, and starts the stream.
        Runs in a thread pool to avoid blocking asyncio event loop with subprocess calls.
        """
        loop = asyncio.get_event_loop()

        try:
            # Run metadata extraction in thread pool (since it uses subprocess)
            handler = await loop.run_in_executor(
                None, self._handler_registry.get_handler, managed.info.source_url
            )

            if handler is None:
                managed.info.status = "error"
                managed.info.error_message = "No handler found for this stream URL"
                return

            # Extract metadata (title, duration, is_live, is_vod)
            metadata = await loop.run_in_executor(
                None, handler.get_metadata, managed.info.source_url
            )

            # Derive name from metadata if user didn't provide one
            if user_name:
                final_name = user_name
            elif metadata and metadata.title:
                final_name = metadata.title
            else:
                # Fallback: extract from URL
                final_name = self._derive_name_from_url(managed.info.source_url)

            # Update stream info with metadata
            managed.info.name = final_name
            managed.info.is_platform_url = handler.__class__.__name__ in (
                "TwitchHandler",
                "YouTubeHandler",
                "AmssKamereHandler",
            )

            if metadata:
                managed.info.is_vod = metadata.is_vod

            # Store handler reference for use during start()
            managed._handler = handler

            # Save updated info to database
            database.save_stream(
                user_id=user_id,
                stream_id=managed.info.id,
                name=final_name,
                source_url=managed.info.source_url,
                status=managed.info.status,
                is_platform_url=managed.info.is_platform_url,
                is_vod=managed.info.is_vod,
            )

            # Now start the stream (this may download VOD, etc.)
            await loop.run_in_executor(None, managed.start)

        except Exception as e:
            logger.exception("Failed to initialize stream %s", managed.info.id)
            managed.info.status = "error"
            managed.info.error_message = str(e)
            database.save_stream(
                user_id=user_id,
                stream_id=managed.info.id,
                name=managed.info.name,
                source_url=managed.info.source_url,
                status="error",
                error_message=str(e),
            )

    def _derive_name_from_url(self, url: str) -> str:
        """Extract a reasonable name from a URL if no title metadata is available."""
        # Remove protocol
        name = re.sub(r"^https?://", "", url)
        # Remove query params
        name = name.split("?")[0]
        # Take last part of path or domain
        parts = name.rstrip("/").split("/")
        name = parts[-1] if len(parts) > 1 else parts[0]
        # Clean up
        name = name.replace("_", " ").replace("-", " ").strip()
        return name[:50] or "Unnamed Stream"

    def add_stream(self, name: str, source_url: str) -> StreamInfo:
        stream_id = uuid.uuid4().hex[:12]

        is_platform = bool(_PLATFORM_RE.search(source_url))
        is_vod = _detect_youtube_vod(source_url)

        info = StreamInfo(
            id=stream_id,
            name=name,
            source_url=source_url,
            is_platform_url=is_platform,
            is_vod=is_vod,
        )
        managed = ManagedStream(info)
        with self._lock:
            self._streams[stream_id] = managed
        managed.start()
        self._save_state()
        return info

    def remove_stream(self, user_id: int, stream_id: str) -> bool:
        with self._lock:
            managed = self._streams.pop((user_id, stream_id), None)
        if managed is None:
            return False
        managed.cleanup()
        database.delete_stream(user_id, stream_id)
        return True

    def get_stream(self, user_id: int, stream_id: str) -> ManagedStream | None:
        return self._streams.get((user_id, stream_id))

    def list_streams(self, user_id: int) -> list[dict]:
        results = []
        with self._lock:
            for (uid, _), m in self._streams.items():
                if uid == user_id:
                    d = asdict(m.info)
                    d["playlist_url"] = m.playlist_url
                    results.append(d)
        return results

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
        for m in streams:
            m.stop()
