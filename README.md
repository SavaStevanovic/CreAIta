# CreAIta – Stream Viewer

A Python-based live-stream viewer with a web frontend.
Feed it any stream URL (RTSP, RTMP, HTTP/HLS, …) and watch it in your browser.

## Architecture

```
┌─────────────┐        ┌───────────────┐        ┌──────────────┐
│  Stream URL  │──────▶│  FFmpeg (HLS)  │──────▶│  FastAPI      │──────▶  Browser
│  (rtsp/rtmp) │        │  transcoder    │  .ts   │  serves HLS   │  HLS.js
└─────────────┘        └───────────────┘        └──────────────┘
```

- **FFmpeg** ingests the source stream and outputs HLS segments (`.ts` + `.m3u8`).
- **FastAPI** serves the REST API, the HLS segments, and the web UI.
- **HLS.js** in the browser plays the live stream with audio.
- Streams are **persisted** in a JSON state file → they survive page refreshes
  _and_ server restarts.

### Future: Deep-Learning Pipeline

The `StreamManager` is designed so you can intercept frames (via OpenCV)
before they're re-encoded.  A `process_frame` callback will let you plug in
any PyTorch / TensorFlow model for real-time inference overlays.

## Prerequisites

| Tool       | Version | Purpose                        |
|------------|---------|--------------------------------|
| Python     | ≥ 3.10  | Runtime                        |
| FFmpeg     | ≥ 5.0   | Stream transcoding             |
| streamlink | latest  | Optional: Twitch/YouTube URLs  |

Make sure `ffmpeg` is on your `PATH`.
For Twitch/YouTube support, install `streamlink`: `sudo apt install streamlink` or `pip install streamlink`.

## Quick Start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Run the server
python run.py
```

Then open **http://localhost:8000** in your browser.

### Adding a stream

1. Enter a **name** and a **stream URL** in the top bar.
2. Click **▶ Add Stream**.
3. After a few seconds FFmpeg will produce the first HLS segments and the
   video player will start.

Example test URLs:

| URL | Description |
|-----|-------------|
| `https://www.twitch.tv/username` | Twitch stream (requires streamlink) |
| `https://www.youtube.com/watch?v=...` | YouTube live (requires streamlink) |
| `http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4` | Big Buck Bunny (HTTP) |
| `rtsp://…` | Any RTSP camera |
| `rtmp://…` | Any RTMP ingest |

### Important Notes

- **Platform streams (Twitch/YouTube)** automatically refresh their tokens every 45 minutes to prevent expiration. Streams will restart seamlessly in the background.
- **Platform streams** are **not auto-restored** after server restart (tokens expire). Re-add them manually via the UI.
- **Direct streams (RTSP/RTMP/HTTP)** are restored automatically after server restart.
- The `streams/` directory contains temporary HLS segments and is excluded from git.

### Supported Platforms

Works with any service supported by `streamlink`:
- ✅ **Twitch** — `https://www.twitch.tv/username`
- ✅ **YouTube Live** — `https://www.youtube.com/watch?v=...` or `https://youtu.be/...`
- ✅ Direct streams — RTSP, RTMP, HTTP/HLS URLs work natively without streamlink

## Project Structure

```
CreAIta/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI application & routes
│   ├── stream_manager.py    # FFmpeg process & stream lifecycle
│   └── static/
│       └── index.html       # Web frontend (HLS.js player)
├── streams/                 # HLS output (auto-created)
├── requirements.txt
├── run.py                   # Server entry-point
└── README.md
```

## License

MIT
