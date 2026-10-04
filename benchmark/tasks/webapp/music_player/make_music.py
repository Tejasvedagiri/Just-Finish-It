"""Writes the music_player task's song library: music/library.json and one
short WAV per song (a 2-second tone, a different pitch each). The files are
committed; this only exists to regenerate them.

They're real, playable audio on purpose: the checker plays them in Chromium
and waits for `ended` to see the player move on by itself, so they're short.

    python benchmark/tasks/webapp/music_player/make_music.py
"""
import json
import math
import struct
import wave
from pathlib import Path

RATE = 8000
SECONDS = 2.0

SONGS = [
    ("s01", "Neon Harbor", "The Glass Pilots", "Low Tide", 262),
    ("s02", "Paper Satellites", "Juniper Static", "Signal Fires", 294),
    ("s03", "Slow Orbit", "Mara Quell", "Driftwood", 330),
    ("s04", "Midnight Ferry", "Neon Wolves", "Crossings", 349),
    ("s05", "Copper Rain", "Juniper Static", "Signal Fires", 392),
    ("s06", "Tidewater", "The Glass Pilots", "Low Tide", 440),
    ("s07", "Lantern Season", "Odette Vance", "Small Hours", 494),
    ("s08", "Kite Weather", "Mara Quell", "Driftwood", 523),
]


def tone(path: Path, hz: int) -> None:
    frames = int(RATE * SECONDS)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        fade = RATE // 20
        data = bytearray()
        for i in range(frames):
            envelope = min(1.0, i / fade, (frames - i) / fade)
            data += struct.pack("<h", int(0.3 * envelope * 32767 * math.sin(2 * math.pi * hz * i / RATE)))
        w.writeframes(bytes(data))


def main() -> None:
    music = Path(__file__).parent / "music"
    music.mkdir(exist_ok=True)
    library = []
    for song_id, title, artist, album, hz in SONGS:
        name = title.lower().replace(" ", "-") + ".wav"
        tone(music / name, hz)
        library.append({"id": song_id, "title": title, "artist": artist, "album": album,
                        "duration": SECONDS, "file": f"music/{name}"})
    (music / "library.json").write_text(json.dumps(library, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
