"""Post-hoc checker for the 'music_player' webapp task -- copied into the
project only AFTER the JFI session ends (never visible to the model), then
run as task.json's verify.command.

It serves the project over HTTP (ES modules don't load from file://) and
drives the real page in headless Chromium through Playwright: what the
player does in a browser, not what its source looks like. Which song is
playing is read from the <audio> element itself (its src, paused and
currentTime), so a player that only updates its labels fails.

Rows are clicked with dispatch_event: the contract is "clicking the song
element plays it", and a real mouse click at the row's centre could land on
the add-to-playlist <select> inside it.

Then `node --test` must pass with at least 3 tests. One PASS/FAIL line per
check; exits 0 iff every check passes.
"""
import functools
import http.server
import json
import re
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path.cwd()
LIBRARY = json.loads((ROOT / "music" / "library.json").read_text(encoding="utf-8"))
IDS = [s["id"] for s in LIBRARY]
BY_ID = {s["id"]: s for s in LIBRARY}
WAIT_MS = 4000

results = []


def check(name, passed, detail=""):
    results.append(bool(passed))
    print(f"{'PASS' if passed else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not passed else ""), flush=True)


def section(name, fn, *args):
    """A section that crashes (a missing element, a timeout) fails as one
    check instead of ending the run, so the other sections still report."""
    try:
        fn(*args)
    except Exception as e:
        check(f"{name} (crashed)", False, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")


class Handler(http.server.SimpleHTTPRequestHandler):
    # Windows' registry can map .js to text/plain, and Chromium refuses an ES
    # module served as that.
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript",
                      ".mjs": "text/javascript", ".json": "application/json", ".wav": "audio/wav",
                      ".css": "text/css", ".html": "text/html"}

    def log_message(self, *args):
        pass


def serve():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(ROOT)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/index.html"


# ---------------------------------------------------------------- page helpers

def ids(page, testid):
    return page.eval_on_selector_all(f'[data-testid="{testid}"]', "els => els.map(e => e.dataset.songId)")


def audio(page):
    return page.evaluate("""() => {
        const a = document.querySelector('[data-testid="audio"]');
        return a ? {paused: a.paused, src: decodeURIComponent(a.currentSrc || a.src || ''), t: a.currentTime,
                    d: a.duration} : null;
    }""")


def playing_id(page):
    state = audio(page)
    if not state or not state["src"]:
        return None
    return next((sid for sid, s in BY_ID.items() if state["src"].endswith(s["file"])), None)


def wait_playing(page, song_id):
    """`song_id` is the <audio>'s source and it is really playing."""
    page.wait_for_function("""([file]) => {
        const a = document.querySelector('[data-testid="audio"]');
        return a && decodeURIComponent(a.currentSrc || a.src || '').endsWith(file) && !a.paused && a.currentTime > 0.05;
    }""", arg=[BY_ID[song_id]["file"]], timeout=WAIT_MS)


def is_playing(page, song_id):
    try:
        wait_playing(page, song_id)
        return True
    except Exception:
        return False


def click_song(page, song_id, testid="song"):
    page.locator(f'[data-testid="{testid}"][data-song-id="{song_id}"]').first.dispatch_event("click")


def press(page, testid):
    page.locator(f'[data-testid="{testid}"]').first.click()


def step(page, testid):
    """next/prev, then the song that's playing after it."""
    before = audio(page)["src"]
    press(page, testid)
    page.wait_for_function("""([before]) => {
        const a = document.querySelector('[data-testid="audio"]');
        return a && decodeURIComponent(a.currentSrc || a.src || '') !== before && !a.paused;
    }""", arg=[before], timeout=WAIT_MS)
    return playing_id(page)


def search(page, text):
    page.locator('[data-testid="search"]').fill(text)
    page.wait_for_timeout(300)
    return ids(page, "song")


def open_page(browser, url, errors):
    context = browser.new_context()
    page = context.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector('[data-testid="song"]', timeout=8000)
    return context, page


# ---------------------------------------------------------------- sections

def library_and_playback(browser, url):
    errors = []
    _, page = open_page(browser, url, errors)
    check("the library lists all 8 songs from music/library.json, in order", ids(page, "song") == IDS,
          f"got {ids(page, 'song')}")
    rows = page.eval_on_selector_all('[data-testid="song"]', "els => els.map(e => [e.dataset.songId, e.innerText])")
    missing = [sid for sid, text in rows if sid in BY_ID and not (BY_ID[sid]["title"] in text and BY_ID[sid]["artist"] in text)]
    check("each song shows its title and artist", rows and not missing, f"missing on {missing}")

    check("search matches a title or an artist, case-insensitive", sorted(search(page, "nEoN")) == ["s01", "s04"],
          f"'nEoN' -> {ids(page, 'song')}")
    check("search matches the album", sorted(search(page, "driftwood")) == ["s03", "s08"],
          f"'driftwood' -> {ids(page, 'song')}")
    check("clearing the search shows every song again", search(page, "") == IDS, f"got {ids(page, 'song')}")

    click_song(page, "s03")
    check("clicking a song plays its file in the <audio> element", is_playing(page, "s03"), f"audio: {audio(page)}")
    now = page.locator('[data-testid="now-playing"]').inner_text()
    check("now-playing shows the title and artist", "Slow Orbit" in now and "Mara Quell" in now, f"got {now!r}")

    press(page, "play-pause")
    page.wait_for_timeout(300)
    check("play-pause pauses", audio(page)["paused"], f"audio: {audio(page)}")
    press(page, "play-pause")
    check("play-pause resumes the same song", is_playing(page, "s03"), f"audio: {audio(page)}")

    order = [step(page, "next"), step(page, "prev"), step(page, "prev"), step(page, "prev"), step(page, "prev")]
    check("next and prev follow the library order", order[:4] == ["s04", "s03", "s02", "s01"], f"got {order}")
    check("prev on the first song goes to the last", order[4] == "s08", f"got {order}")
    check("next on the last song goes to the first", step(page, "next") == "s01", f"playing {playing_id(page)}")

    page.evaluate("""() => { const a = document.querySelector('[data-testid="audio"]');
                             a.currentTime = Math.max(0, a.duration - 0.3); }""")
    check("when a song ends the next one starts by itself", is_playing(page, "s02"), f"audio: {audio(page)}")

    search(page, "glass")
    click_song(page, "s06")
    wait_playing(page, "s06")
    check("a song started from search results queues only the results", step(page, "next") == "s01",
          f"next after s06 in 'glass' results played {playing_id(page)}")
    check("the page raised no JavaScript errors", not errors, "; ".join(errors[:3]))


def shuffle(browser, url):
    errors = []
    _, page = open_page(browser, url, errors)
    button = page.locator('[data-testid="shuffle"]').first
    check("shuffle starts off (aria-pressed=false)", button.get_attribute("aria-pressed") == "false",
          f"aria-pressed={button.get_attribute('aria-pressed')!r}")
    click_song(page, "s05")
    wait_playing(page, "s05")

    orders = []
    for _ in range(3):
        button.click()
        page.wait_for_timeout(200)
        pressed = button.get_attribute("aria-pressed")
        orders.append(["s05"] + [step(page, "next") for _ in range(7)])
        button.click()
        page.wait_for_timeout(200)
        if pressed != "true" or button.get_attribute("aria-pressed") != "false":
            check("shuffle toggles aria-pressed", False, f"on={pressed!r} off={button.get_attribute('aria-pressed')!r}")
            return
        # back to s05 for the next round, in normal order
        click_song(page, "s05")
        wait_playing(page, "s05")
    check("shuffle toggles aria-pressed", True)
    whole = [o for o in orders if sorted(o) == sorted(IDS)]
    check("shuffled, every song plays once starting from the current one", len(whole) == 3,
          f"orders: {orders}")
    rotation = IDS[IDS.index("s05"):] + IDS[:IDS.index("s05")]
    check("each shuffle is a new random order", len({tuple(o) for o in orders}) > 1 and
          any(o != rotation for o in orders), f"orders: {orders}")

    button.click()
    page.wait_for_timeout(200)
    current = step(page, "next")
    button.click()
    page.wait_for_timeout(200)
    expected = IDS[(IDS.index(current) + 1) % len(IDS)]
    check("turning shuffle off continues in library order from the current song", step(page, "next") == expected,
          f"after {current} expected {expected}, played {playing_id(page)}")
    check("the page raised no JavaScript errors", not errors, "; ".join(errors[:3]))


def playlist_names(page):
    return page.eval_on_selector_all('[data-testid="playlist"]', "els => els.map(e => e.dataset.playlistName)")


def create(page, name):
    page.locator('[data-testid="playlist-name"]').fill(name)
    press(page, "create-playlist")
    page.wait_for_timeout(250)


def add_to(page, song_id, name):
    press(page, "show-library")
    page.wait_for_timeout(200)
    page.locator(f'[data-testid="song"][data-song-id="{song_id}"] [data-testid="add-to-playlist"]').select_option(value=name)
    page.wait_for_timeout(200)


def open_playlist(page, name):
    page.locator(f'[data-testid="playlist"][data-playlist-name="{name}"]').first.dispatch_event("click")
    page.wait_for_timeout(300)
    return ids(page, "playlist-song")


def playlists(browser, url):
    errors = []
    context, page = open_page(browser, url, errors)
    create(page, "Road Trip")
    check("create-playlist adds a playlist", playlist_names(page) == ["Road Trip"], f"got {playlist_names(page)}")
    create(page, "")
    create(page, "Road Trip")
    check("an empty or existing name creates nothing", playlist_names(page) == ["Road Trip"],
          f"got {playlist_names(page)}")
    create(page, "Focus")
    for sid in ("s05", "s02", "s07", "s05"):
        add_to(page, sid, "Road Trip")
    add_to(page, "s08", "Focus")
    check("an opened playlist lists its songs in the order added, no duplicates",
          open_playlist(page, "Road Trip") == ["s05", "s02", "s07"], f"got {ids(page, 'playlist-song')}")

    click_song(page, "s02", "playlist-song")
    played = [playing_id(page) if is_playing(page, "s02") else None, step(page, "next"), step(page, "next"),
              step(page, "prev")]
    check("a song started from a playlist queues only that playlist", played == ["s02", "s07", "s05", "s07"],
          f"got {played}")

    page.locator('[data-testid="playlist-song"][data-song-id="s02"] [data-testid="remove-from-playlist"]').click()
    page.wait_for_timeout(250)
    check("remove-from-playlist removes the song", ids(page, "playlist-song") == ["s05", "s07"],
          f"got {ids(page, 'playlist-song')}")

    page.reload()
    page.wait_for_selector('[data-testid="song"]', timeout=8000)
    page.wait_for_timeout(300)
    check("playlists are still there after a reload", sorted(playlist_names(page)) == ["Focus", "Road Trip"],
          f"got {playlist_names(page)}")
    check("a reloaded playlist keeps its songs", open_playlist(page, "Road Trip") == ["s05", "s07"],
          f"got {ids(page, 'playlist-song')}")

    open_playlist(page, "Focus")
    press(page, "delete-playlist")
    page.wait_for_timeout(250)
    gone = playlist_names(page) == ["Road Trip"]
    page.reload()
    page.wait_for_selector('[data-testid="song"]', timeout=8000)
    page.wait_for_timeout(300)
    check("delete-playlist deletes it, also after a reload", gone and playlist_names(page) == ["Road Trip"],
          f"got {playlist_names(page)}")
    check("the page raised no JavaScript errors", not errors, "; ".join(errors[:3]))
    context.close()


def node_tests():
    try:
        run = subprocess.run(["node", "--test", "--test-reporter=tap"], cwd=ROOT, capture_output=True, text=True,
                             timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        check("node --test passes with at least 3 tests", False, str(e))
        return
    passed = re.search(r"^# pass (\d+)", run.stdout, re.M)
    failed = re.search(r"^# fail (\d+)", run.stdout, re.M)
    count = int(passed.group(1)) if passed else 0
    check("node --test passes with at least 3 tests",
          run.returncode == 0 and count >= 3 and failed is not None and failed.group(1) == "0",
          f"exit {run.returncode}, pass {count}, fail {failed.group(1) if failed else '?'}: "
          + (run.stdout + run.stderr).strip()[-300:])


def main():
    if not (ROOT / "index.html").is_file():
        print("FAIL: index.html exists at the project root")
        sys.exit(1)
    server, url = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            section("library and playback", library_and_playback, browser, url)
            section("shuffle", shuffle, browser, url)
            section("playlists", playlists, browser, url)
            browser.close()
    finally:
        server.shutdown()
    node_tests()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
