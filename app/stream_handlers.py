"""
Stream handlers — abstraction layer for different stream sources.
Uses dependency injection pattern for extensibility.
"""

from __future__ import annotations

import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StreamMetadata:
    """Metadata extracted from a stream source."""
    title: Optional[str] = None
    duration: Optional[float] = None
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
        try:
            result = subprocess.run(
                ["yt-dlp", "--print", "%(title)s|%(is_live)s|%(duration)s",
                 "--no-warnings", "--no-playlist", url],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split("|")
                title = parts[0] if len(parts) > 0 else None
                is_live_str = parts[1] if len(parts) > 1 else "False"
                duration_str = parts[2] if len(parts) > 2 else "0"
                
                is_live = is_live_str.lower() in ("true", "1")
                try:
                    duration = float(duration_str) if duration_str and duration_str != "None" else None
                except ValueError:
                    duration = None
                
                return StreamMetadata(
                    title=title,
                    duration=duration,
                    is_live=is_live,
                    is_vod=not is_live and duration is not None,
                )
        except Exception as e:
            logger.warning("Failed to extract YouTube metadata: %s", e)
        
        return StreamMetadata(is_live=False, is_vod=True)

    def get_feeder_command(self, url: str) -> list[str]:
        return [
            "yt-dlp", "-f", "best",
            "--throttled-rate", "100K",
            "-o", "-", url,
        ]

    def get_ffmpeg_input_args(self, url: str) -> tuple[list[str], str]:
        # YouTube uses pipe mode for both live and VOD piping
        return ([], "pipe:0")


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
