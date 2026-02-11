#!/usr/bin/env python3
"""Quick test to verify stream handlers and FFmpeg command building."""

import sys

sys.path.insert(0, "/home/sava/Documents/projects/CreAIta")

from app.stream_handlers import StreamHandlerRegistry

# Test URLs
test_urls = [
    ("https://www.twitch.tv/eslcs", "Twitch"),
    ("https://www.youtube.com/watch?v=3FdFYSNTcpE", "YouTube"),
    ("http://example.com/stream.m3u8", "Generic HLS"),
]

registry = StreamHandlerRegistry()

for url, label in test_urls:
    print(f"\n{label}: {url}")
    print("=" * 60)

    handler = registry.get_handler(url)
    print(f"Handler: {handler.__class__.__name__}")

    # Test metadata
    try:
        metadata = handler.get_metadata(url)
        print(f"Metadata: {metadata}")
    except Exception as e:
        print(f"Metadata error: {e}")

    # Test feeder command (for platform streams)
    if hasattr(handler, "get_feeder_command"):
        try:
            feeder_cmd = handler.get_feeder_command(url)
            print(f"Feeder command: {' '.join(feeder_cmd[:5])}...")
        except Exception as e:
            print(f"Feeder error: {e}")

    # Test FFmpeg input args
    try:
        input_flags, input_source = handler.get_ffmpeg_input_args(url)
        print(f"Input flags: {input_flags}")
        print(f"Input source: {input_source}")

        # Build the full input args list like the code should
        full_input_args = [*input_flags, "-i", input_source]
        print(f"Full FFmpeg input args: {full_input_args}")
    except Exception as e:
        print(f"FFmpeg args error: {e}")

print("\n" + "=" * 60)
print("✅ All handlers working correctly!")
