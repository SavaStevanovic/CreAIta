"""
Stream handlers — abstraction layer for different stream sources.
Uses dependency injection pattern for extensibility.
"""

from __future__ import annotations

import logging
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StreamMetadata:
    """Metadata extracted from a stream source."""

    title: str | None = None
    duration: float | None = None
    is_live: bool = True
    is_vod: bool = False


class StreamHandler(ABC):
    """Abstract base class for stream handlers."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Check if this handler can process the given URL."""
        pass

    @abstractmethod
    def get_metadata(self, url: str) -> StreamMetadata:
        """Extract metadata from the stream source (non-blocking)."""
        pass

    @abstractmethod
    def get_feeder_command(self, url: str) -> list[str]:
        """Get the command to pipe stream data."""
        pass

    @abstractmethod
    def get_ffmpeg_input_args(self, url: str) -> tuple[list[str], str]:
        """Get FFmpeg input args. Returns (flags, input_source)."""
        pass


class TwitchHandler(StreamHandler):
    """Handler for Twitch live streams."""

    def can_handle(self, url: str) -> bool:
        return "twitch.tv/" in url.lower()

    def get_metadata(self, url: str) -> StreamMetadata:
        """Extract Twitch stream metadata."""
        try:
            # Get stream title from streamlink
            result = subprocess.run(
                ["streamlink", "--json", url],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                import json

                data = json.loads(result.stdout)
                metadata = data.get("metadata", {})
                return StreamMetadata(
                    title=metadata.get("title"),
                    is_live=True,
                    is_vod=False,
                )
        except Exception as e:
            logger.warning("Failed to extract Twitch metadata: %s", e)

        return StreamMetadata(is_live=True, is_vod=False)

    def get_feeder_command(self, url: str) -> list[str]:
        return ["streamlink", "--stdout", url, "best"]

    def get_ffmpeg_input_args(self, url: str) -> tuple[list[str], str]:
        # Twitch uses pipe mode
        return ([], "pipe:0")


class YouTubeHandler(StreamHandler):
    """Handler for YouTube streams (both live and VOD)."""

    def can_handle(self, url: str) -> bool:
        return "youtube.com/" in url.lower() or "youtu.be/" in url.lower()

    def get_metadata(self, url: str) -> StreamMetadata:
        """Extract YouTube stream metadata."""
        # Try multiple strategies for metadata extraction (bot detection workaround)
        strategies = [
            [],
            ["--cookies-from-browser", "chrome"],
        ]

        for extra_args in strategies:
            try:
                result = subprocess.run(
                    [
                        "yt-dlp",
                        "--js-runtimes",
                        "node",
                        *extra_args,
                        "--print",
                        "%(title)s|%(is_live)s|%(duration)s",
                        "--no-warnings",
                        "--no-playlist",
                        "--no-download",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts = result.stdout.strip().split("|")
                    title = parts[0] if len(parts) > 0 else None
                    is_live_str = parts[1] if len(parts) > 1 else "False"
                    duration_str = parts[2] if len(parts) > 2 else "0"

                    is_live = is_live_str.lower() in ("true", "1")
                    try:
                        duration = (
                            float(duration_str) if duration_str and duration_str != "None" else None
                        )
                    except ValueError:
                        duration = None

                    return StreamMetadata(
                        title=title,
                        duration=duration,
                        is_live=is_live,
                        is_vod=not is_live and duration is not None,
                    )
                # If bot detection, try next strategy
                if "Sign in to confirm" in result.stderr:
                    logger.info("YouTube bot detection during metadata, trying next strategy")
                    continue
                break  # Other error, don't retry
            except Exception as e:
                logger.warning("Failed to extract YouTube metadata: %s", e)
                break

        # Default: assume VOD (most YouTube links are VODs)
        return StreamMetadata(is_live=False, is_vod=True)

    def get_feeder_command(self, url: str) -> list[str]:
        return [
            "yt-dlp",
            "--js-runtimes",
            "node",
            "-f",
            "best",
            "--throttled-rate",
            "100K",
            "-o",
            "-",
            url,
        ]

    def get_ffmpeg_input_args(self, url: str) -> tuple[list[str], str]:
        # YouTube uses pipe mode for both live and VOD piping
        return ([], "pipe:0")


class AmssKamereHandler(StreamHandler):
    """Handler for AMSS road cameras at kamere.amss.org.rs.

    Stream URL format: https://kamere.amss.org.rs/{camera_id}/{camera_id}.m3u8
    Requires a Cloudflare cf_clearance cookie from Chrome browser.
    """

    _CAMERA_NAMES: dict[str, str] = {
        "horgos1": "Horgoš E-75 (Entry to Serbia from Hungary)",
        "horgos2": "Horgoš E-75 (Exit from Serbia to Hungary)",
        "batrovci1": "Batrovci E-70 (Entry to Serbia from Croatia)",
        "batrovci2": "Batrovci E-70 (Exit from Serbia to Croatia)",
        "gradina1": "Gradina E-80 (Entry to Serbia from Bulgaria)",
        "gradina2": "Gradina E-80 (Exit from Serbia to Bulgaria)",
        "presevo1": "Preševo E-75 (Entry to Serbia from N. Macedonia)",
        "presevo2": "Preševo E-75 (Exit from Serbia to N. Macedonia)",
    }
    _UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    )
    _REFERER = "https://kamere.amss.org.rs/"

    def can_handle(self, url: str) -> bool:
        return "kamere.amss.org.rs" in url.lower()

    def _extract_camera_id(self, url: str) -> str | None:
        """Return the camera ID embedded in the URL (e.g. 'horgos1')."""
        m = re.search(r"kamere\.amss\.org\.rs/([a-z0-9]+)", url.lower())
        return m.group(1) if m else None

    def _resolve_stream_url(self, url: str) -> str:
        """Produce the HLS .m3u8 URL for a given camera page or direct URL."""
        if url.endswith(".m3u8"):
            return url
        cam_id = self._extract_camera_id(url)
        if cam_id:
            return f"https://kamere.amss.org.rs/{cam_id}/{cam_id}.m3u8"
        raise ValueError(
            f"Cannot determine camera ID from URL '{url}'. "
            "Use a URL like https://kamere.amss.org.rs/horgos1/horgos1.m3u8"
        )

    def _get_cf_clearance(self) -> str:
        """Extract the Cloudflare cf_clearance cookie from Chrome."""

        cookie_file = "/tmp/_amss_cf_cookies.txt"
        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "--cookies-from-browser",
                    "chrome",
                    "--cookies",
                    cookie_file,
                    "--skip-download",
                    "--no-warnings",
                    "--quiet",
                    "https://kamere.amss.org.rs/",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as e:
            logger.warning("Failed to export AMSS cookies: %s", e)
            return ""
        try:
            with open(cookie_file) as fh:
                m = re.search(r"cf_clearance\s+(\S+)", fh.read())
                return m.group(1) if m else ""
        except OSError:
            return ""

    def get_metadata(self, url: str) -> StreamMetadata:
        cam_id = self._extract_camera_id(url)
        if cam_id:
            name = self._CAMERA_NAMES.get(cam_id, f"AMSS Camera {cam_id}")
        else:
            name = "AMSS Road Camera"
        return StreamMetadata(title=name, is_live=True, is_vod=False)

    def get_feeder_command(self, url: str) -> list[str]:
        # AMSS uses direct FFmpeg mode (no separate feeder process)
        return []

    def get_ffmpeg_input_args(self, url: str) -> tuple[list[str], str]:
        stream_url = self._resolve_stream_url(url)
        cf_clearance = self._get_cf_clearance()

        cookie_str = f"cf_clearance={cf_clearance}" if cf_clearance else ""
        headers = f"User-Agent: {self._UA}\r\n" f"Referer: {self._REFERER}\r\n"
        if cookie_str:
            headers += f"Cookie: {cookie_str}\r\n"

        input_flags = [
            "-headers",
            headers,
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        return (input_flags, stream_url)


class GenericHandler(StreamHandler):
    """Handler for generic RTSP/RTMP/HTTP streams."""

    def can_handle(self, url: str) -> bool:
        # Fallback handler for everything else
        return True

    def get_metadata(self, url: str) -> StreamMetadata:
        # Generic streams don't have extractable metadata
        return StreamMetadata(is_live=True, is_vod=False)

    def get_feeder_command(self, url: str) -> list[str]:
        # Generic streams don't use a feeder
        return []

    def get_ffmpeg_input_args(self, url: str) -> tuple[list[str], str]:
        # Direct FFmpeg input with reconnect flags
        input_flags = []
        if url.startswith("http"):
            input_flags += ["-reconnect", "1", "-reconnect_delay_max", "5"]
            if ".m3u8" in url or "playlist" in url:
                input_flags += ["-reconnect_streamed", "1"]

        return (input_flags, url)


class StreamHandlerRegistry:
    """Registry for stream handlers with dependency injection."""

    def __init__(self):
        self._handlers: list[StreamHandler] = [
            TwitchHandler(),
            YouTubeHandler(),
            AmssKamereHandler(),
            GenericHandler(),  # Must be last (fallback)
        ]

    def get_handler(self, url: str) -> StreamHandler:
        """Get the appropriate handler for a URL."""
        for handler in self._handlers:
            if handler.can_handle(url):
                return handler
        # Should never reach here since GenericHandler accepts everything
        return self._handlers[-1]

    def add_handler(self, handler: StreamHandler, priority: int = 0):
        """Add a custom handler with optional priority."""
        self._handlers.insert(priority, handler)
