#!/usr/bin/env python3
import yt_dlp

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "skip_download": True,
    "noplaylist": True,
    "postprocessors": [],
    "source_address": "0.0.0.0",
    "sleep_interval": 2,
    "max_sleep_interval": 10,
    "retry_retries": 5,
    "fragment_retries": 5,
    "nocheckcertificate": True,
    "simulate": False,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "dont_merge_formats": True,
    "prefer_ffmpeg": False,
    "keep_original_format": True,
    "geo_bypass": True,
}

ydl = yt_dlp.YoutubeDL(YDL_OPTS)

print("Searching YouTube for 'lofi 2024'...")
try:
    info = ydl.extract_info("ytsearch:lofi 2024", download=False)
    if "entries" in info:
        info = info["entries"][0]
    title = info.get("title", "Unknown")
    url = info.get("url", "")
    print(f"Title: {title}")
    print(f"URL found: {url[:80] if url else 'NO URL'}")
    if url and "expire=" in url:
        print("URL contains expire parameter")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")