"""
Music / Radio Module for Gorpo.
Uses yt-dlp + PyAV (no system ffmpeg) to stream audio to Discord voice.
Single command: /radio play <query|url> | stop | np | queue
"""

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional, List, Set

import discord
from discord import app_commands
import yt_dlp
import av

from bot.config import config
from bot.llm import simple_complete


# ──────────────────────────────────────────────────────────────────────────────
# Temp-file tracking (downloaded audio, cleaned up on source cleanup)
# ──────────────────────────────────────────────────────────────────────────────
_temp_files: Set[str] = set()


def _register_temp_file(path: str) -> None:
    _temp_files.add(path)


def _cleanup_temp_files() -> None:
    for path in list(_temp_files):
        try:
            os.remove(path)
        except OSError:
            pass
        _temp_files.discard(path)


# ──────────────────────────────────────────────────────────────────────────────
# Audio Source (PCM → Discord encodes via loaded libopus)
# ──────────────────────────────────────────────────────────────────────────────
class YTDLPSource(discord.AudioSource):
    """
    Streams audio from yt-dlp → PyAV → PCM frames (20ms, 48kHz, stereo, s16).
    Discord.py will encode PCM to Opus using the loaded libopus.
    """

    def __init__(self, url: str, title: str, webpage_url: str, volume: float = 0.5):
        self.url = url
        self.title = title
        self.webpage_url = webpage_url
        self.volume = volume
        self._container: Optional[av.container.InputContainer] = None
        self._resampler: Optional[av.AudioResampler] = None
        self._frame_iter = None
        self._buffer = bytearray()
        self._eof = False
        self._init_container()

    def _init_container(self) -> None:
        # Open with PyAV (uses bundled FFmpeg)
        self._container = av.open(self.url, mode="r", options={"timeout": "5000000"})
        audio_streams = [s for s in self._container.streams if s.type == "audio"]
        if not audio_streams:
            raise RuntimeError("No audio stream found")
        stream = audio_streams[0]

        # Resampler: 48kHz stereo s16 (Discord's native format)
        self._resampler = av.AudioResampler(
            format="s16",
            layout="stereo",
            rate=48000,
        )
        # Create frame iterator
        self._frame_iter = self._container.decode(audio=0)

    def read(self) -> bytes:
        """Return exactly 3840 bytes (20ms of 48kHz stereo s16) or empty bytes on EOF."""
        FRAME_SIZE = 3840  # OpusEncoder.FRAME_SIZE = 960 samples * 2 channels * 2 bytes

        while len(self._buffer) < FRAME_SIZE:
            if self._eof:
                break
            try:
                frame = next(self._frame_iter)
            except StopIteration:
                self._eof = True
                break
            except Exception:
                self._eof = True
                break

            # Resample frame
            try:
                resampled = self._resampler.resample(frame)
                for rframe in resampled:
                    # Extract raw PCM bytes without numpy (to_ndarray()
                    # requires numpy, which isn't in this portable venv).
                    # Iterate planes and strip alignment padding to get
                    # clean s16 samples (samples * channels * 2 bytes).
                    plane_data = b"".join(bytes(p) for p in rframe.planes)
                    channels = len(rframe.layout.channels) if rframe.layout else 2
                    clean_len = rframe.samples * channels * 2
                    self._buffer.extend(plane_data[:clean_len])
            except Exception:
                continue

        if len(self._buffer) >= FRAME_SIZE:
            chunk = bytes(self._buffer[:FRAME_SIZE])
            del self._buffer[:FRAME_SIZE]
            # Apply volume (simple scaling)
            if self.volume != 1.0:
                import array
                arr = array.array('h', chunk)
                for i in range(len(arr)):
                    arr[i] = int(arr[i] * self.volume)
                chunk = arr.tobytes()
            return chunk

        # EOF - return remaining (padded with silence if needed)
        if self._buffer:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            if len(chunk) < FRAME_SIZE:
                chunk += b"\x00" * (FRAME_SIZE - len(chunk))
            return chunk

        return b""

    def is_opus(self) -> bool:
        return False  # We output PCM, discord.py encodes

    def cleanup(self) -> None:
        try:
            if self._container:
                self._container.close()
        except Exception:
            pass
        # Remove the temp audio file if we downloaded one
        if self.url and os.path.exists(self.url) and self.url in _temp_files:
            try:
                os.remove(self.url)
            except OSError:
                pass
            _temp_files.discard(self.url)


# ──────────────────────────────────────────────────────────────────────────────
# Queue & Player State
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Track:
    title: str
    url: str
    webpage_url: str
    duration: Optional[int] = None
    requester: Optional[discord.User] = None


@dataclass
class GuildMusicState:
    voice_client: Optional[discord.VoiceClient] = None
    queue: List[Track] = field(default_factory=list)
    current: Optional[Track] = None
    volume: float = 0.5
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    text_channel: Optional[discord.TextChannel] = None


_guild_states: dict[int, GuildMusicState] = {}


def get_guild_state(guild_id: int) -> GuildMusicState:
    if guild_id not in _guild_states:
        _guild_states[guild_id] = GuildMusicState()
    return _guild_states[guild_id]


# ──────────────────────────────────────────────────────────────────────────────
# yt-dlp Extraction
# ──────────────────────────────────────────────────────────────────────────────
YTDL_OPTS = {
    "format": "bestaudio",  # audio-only, no ffmpeg merge needed
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "skip_download": True,
    "noplaylist": True,
    "postprocessors": [],  # No ffmpeg post-processing
    "source_address": "0.0.0.0",  # Force IPv4
    "sleep_interval": 2,  # Be polite to YouTube
    "max_sleep_interval": 10,
    "retries": 5,  # Retry on bot-detection / 429 errors
    "extractor_retries": 3,
    "fragment_retries": 5,
    "nocheckcertificate": True,
    "simulate": False,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "dont_merge_formats": True,
    "prefer_ffmpeg": False,
    "keep_original_format": True,
    "geo_bypass": True,
}

# Optional cookies file for YouTube bot-detection bypass.
# User can export YouTube cookies to gorpo/cookies.txt (via browser extension
# "cookies.txt" or similar) to sign in and avoid bot-verification pages.
_COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cookies.txt")
_COOKIES_FILE = os.path.abspath(_COOKIES_FILE)


async def extract_info(query: str) -> Track:
    """Search or extract direct URL via yt-dlp."""
    loop = asyncio.get_event_loop()

    def _extract() -> Track:
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            # If it's a URL, extract directly; else search
            if re.match(r"^https?://", query):
                info = ydl.extract_info(query, download=False)
            else:
                info = ydl.extract_info(f"ytsearch:{query}", download=False)
                if "entries" in info:
                    info = info["entries"][0]

            # Download audio to a temp file so PyAV can open it locally.
            # av.open() cannot handle YouTube's googlevideo stream URLs ([Errno 5]),
            # but reads local files just fine.
            tmp_dir = tempfile.gettempdir()
            outtmpl = os.path.join(tmp_dir, "gorpo_dl_%(id)s.%(ext)s")
            dl_opts = dict(YTDL_OPTS)
            dl_opts["outtmpl"] = outtmpl
            dl_opts["skip_download"] = False  # actually download this time
            # Use cookies file if present (helps bypass YouTube bot detection)
            if os.path.exists(_COOKIES_FILE):
                dl_opts["cookiefile"] = _COOKIES_FILE
            with yt_dlp.YoutubeDL(dl_opts) as ydl2:
                ydl2.download([info.get("webpage_url", query)])
                tmp_path = ydl2.prepare_filename(info)

            _register_temp_file(tmp_path)

            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                raise RuntimeError("Downloaded audio file is missing or empty")

            return Track(
                title=info.get("title", "Unknown"),
                url=tmp_path,
                webpage_url=info.get("webpage_url", query),
                duration=info.get("duration"),
            )

    return await loop.run_in_executor(None, _extract)


# ──────────────────────────────────────────────────────────────────────────────
# Playback Control
# ──────────────────────────────────────────────────────────────────────────────
async def play_next(guild_id: int) -> None:
    state = get_guild_state(guild_id)
    async with state.lock:
        if not state.queue:
            state.current = None
            return

        state.current = state.queue.pop(0)
        track = state.current

    # Try to create source, with retries
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            source = YTDLPSource(track.url, track.title, track.webpage_url, state.volume)
            break  # Success, exit retry loop
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(1)  # Brief pause before retry
                continue
            # All retries failed
            if state.text_channel:
                await state.text_channel.send(f"⚠️ Failed to play **{track.title}**: {e}")
            await play_next(guild_id)
            return

    def after_playing(error: Optional[Exception]) -> None:
        if error:
            print(f"[Radio] Playback error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), discord.client._get_loop())

    if state.voice_client and state.voice_client.is_connected():
        state.voice_client.play(source, after=after_playing)

        if state.text_channel:
            dur = f" ({track.duration//60}:{track.duration%60:02d})" if track.duration else ""
            await state.text_channel.send(f"🎵 **Now Playing:** [{track.title}]({track.webpage_url}){dur}")


async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """Join the user's voice channel or return existing connection."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ You must be in a voice channel.", ephemeral=True)
        return None

    state = get_guild_state(interaction.guild_id)
    state.text_channel = interaction.channel  # type: ignore

    if state.voice_client and state.voice_client.is_connected():
        if state.voice_client.channel != interaction.user.voice.channel:
            await state.voice_client.move_to(interaction.user.voice.channel)
        return state.voice_client

    try:
        vc = await interaction.user.voice.channel.connect()
        state.voice_client = vc
        return vc
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to join voice: {e}", ephemeral=True)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Slash Commands
# ──────────────────────────────────────────────────────────────────────────────
class RadioGroup(app_commands.Group):
    """Radio / Music commands."""

    def __init__(self):
        super().__init__(name="radio", description="Music playback commands")

    @app_commands.command(name="play", description="Play a song (search or URL)")
    @app_commands.describe(query="Song name, YouTube URL, or search query")
    async def play(self, interaction: discord.Interaction, query: str):
        vc = await ensure_voice(interaction)
        if not vc:
            return

        await interaction.response.defer(ephemeral=False)

        try:
            track = await extract_info(query)
            track.requester = interaction.user
        except Exception as e:
            await interaction.followup.send(f"❌ Could not find/play: {e}")
            return

        state = get_guild_state(interaction.guild_id)
        async with state.lock:
            state.queue.append(track)
            pos = len(state.queue)

        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(f"➕ **Queued #{pos}:** [{track.title}]({track.webpage_url})")
        else:
            await interaction.followup.send(f"🎵 **Playing:** [{track.title}]({track.webpage_url})")
            await play_next(interaction.guild_id)

    @app_commands.command(name="stop", description="Stop playback and clear queue")
    async def stop(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        async with state.lock:
            state.queue.clear()
            state.current = None
            if state.voice_client and state.voice_client.is_connected():
                state.voice_client.stop()
                await state.voice_client.disconnect()
                state.voice_client = None
        await interaction.response.send_message("⏹️ Stopped and disconnected.")

    @app_commands.command(name="skip", description="Skip current track")
    async def skip(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped.")
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            await interaction.response.send_message("⏸️ Paused.")
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("Nothing paused.", ephemeral=True)

    @app_commands.command(name="np", description="Show now playing")
    async def now_playing(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        if state.current:
            dur = f" ({state.current.duration//60}:{state.current.duration%60:02d})" if state.current.duration else ""
            req = f" — requested by {state.current.requester.mention}" if state.current.requester else ""
            await interaction.response.send_message(f"🎵 **Now Playing:** [{state.current.title}]({state.current.webpage_url}){dur}{req}")
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="queue", description="Show queue")
    async def show_queue(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        if not state.queue and not state.current:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return

        lines = []
        if state.current:
            lines.append(f"🎵 **Now:** [{state.current.title}]({state.current.webpage_url})")
        for i, track in enumerate(state.queue[:10], 1):
            lines.append(f"`{i}.` [{track.title}]({track.webpage_url})")
        if len(state.queue) > 10:
            lines.append(f"... and {len(state.queue) - 10} more")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="volume", description="Set volume (0-100)")
    @app_commands.describe(level="Volume percentage (0-100)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        state = get_guild_state(interaction.guild_id)
        state.volume = level / 100.0
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume  # type: ignore
        await interaction.response.send_message(f"🔊 Volume set to {level}%")

    @app_commands.command(name="leave", description="Disconnect from voice")
    async def leave(self, interaction: discord.Interaction):
        state = get_guild_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_connected():
            await state.voice_client.disconnect()
            state.voice_client = None
            state.queue.clear()
            state.current = None
            await interaction.response.send_message("👋 Disconnected.")
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)


# ──────────────────────────────────────────────────────────────────────────────
# Setup Function
# ──────────────────────────────────────────────────────────────────────────────
def setup_music_commands(tree: app_commands.CommandTree) -> None:
    tree.add_command(RadioGroup())