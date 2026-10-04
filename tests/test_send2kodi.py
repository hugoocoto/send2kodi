"""Tests: the real send2kodi script against a mock Kodi (mockkodi.py).

    python3 -m unittest discover tests      # no dependencies; a few minutes
    pytest -n auto tests                    # the same, in parallel

Nothing talks to a real Kodi or to the internet: "remote" links point to a
small web server the tests run themselves.
"""

import functools
import http.client
import http.server
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

sys.dont_write_bytecode = True  # loading the script would leave a __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mockkodi import MockKodi  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "send2kodi")
INSTALLER = os.path.join(REPO, "install.sh")

JPEG = b"\xff\xd8\xff\xe0" + bytes(2000)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(2000)
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + bytes(2000)
VIDEO = b"\x1a\x45\xdf\xa3" + os.urandom(200_000)
MP3 = b"ID3" + bytes(5000)
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def load_script():
    loader = importlib.machinery.SourceFileLoader("send2kodi", SCRIPT)
    spec = importlib.util.spec_from_loader("send2kodi", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


s2k = load_script()


class Media(http.server.BaseHTTPRequestHandler):
    """The "internet": links to media, a web page, pictures without an extension."""

    pages = {
        "/clip.mp4": ("video/mp4", VIDEO),
        "/stream": ("video/mp4", VIDEO),
        "/live.m3u8": ("application/vnd.apple.mpegurl", b"#EXTM3U\n"),
        "/hls": ("application/x-mpegURL", b"#EXTM3U\n"),
        "/page": ("text/html; charset=utf-8", b"<html><body>a video page</body></html>"),
        "/img": ("image/png", PNG),
        "/pic.jpg": ("image/jpeg", JPEG),
        "/song": ("audio/mpeg", MP3),
        "/nohead/x": ("video/webm", VIDEO),  # refuses HEAD
    }

    def log_message(self, format, *args):
        pass

    def do_HEAD(self):
        self.answer(body=False)

    def do_GET(self):
        self.answer(body=True)

    def answer(self, body):
        page = self.pages.get(self.path.split("?")[0])
        if page is None or not body and self.path.startswith("/nohead"):
            self.send_error(404 if page is None else 405)
            return
        self.send_response(200)
        self.send_header("Content-Type", page[0])
        self.send_header("Content-Length", str(len(page[1])))
        self.end_headers()
        if body:
            try:
                self.wfile.write(page[1])
            except OSError:
                pass


@functools.cache
def web():
    """The base URL of the "internet"."""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Media)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}"


def name(url):
    return urllib.parse.unquote(url.rsplit("/", 1)[-1])


def alive(pid):
    """Not exited, nor a zombie no one reaps (in a container, say)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_for(condition, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


class Case(unittest.TestCase):
    """Each test gets its own Kodi and its own files."""

    def setUp(self):
        self.kodi = MockKodi()
        self.addCleanup(self.kodi.close)
        self.dir = tempfile.mkdtemp(prefix="send2kodi-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = self.file("empty.cfg", b"")

    def file(self, path, data=b"x"):
        path = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def start(self, *args, stdin=False, env=None):
        environ = {k: v for k, v in os.environ.items()
                   if k not in s2k.DEFAULTS and k != "XDG_CONFIG_HOME"}
        environ.update(KODI_HOST="127.0.0.1", KODI_PORT=str(self.kodi.port),
                       SERVE_PORT="0", START_TIMEOUT="10", PYTHONDONTWRITEBYTECODE="1")
        environ.update(env or {})
        p = subprocess.Popen([sys.executable, SCRIPT, "-c", self.config, *args],
                             stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=environ)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def finish(self, p, timeout=60):
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            self.fail(f"still running after {timeout}s\nstdout:\n{out}\nstderr:\n{err}")
        return p.returncode, out, err

    def send(self, *args, timeout=60, env=None):
        return self.finish(self.start(*args, env=env), timeout)

    def assertSent(self, result, *texts, code=0):
        rc, out, err = result
        self.assertEqual(rc, code, f"stdout:\n{out}\nstderr:\n{err}")
        for text in texts:
            self.assertIn(text, out + err)
        self.assertNotIn("Traceback", err)
        return out, err


class Helpers(unittest.TestCase):
    def test_natural_order(self):
        names = ["Episode 10.mkv", "episode 2.mkv", "Episode 1.mkv", "S01E10.mkv", "s01e02.mkv"]
        self.assertEqual(sorted(names, key=s2k.natural),
                         ["Episode 1.mkv", "episode 2.mkv", "Episode 10.mkv",
                          "s01e02.mkv", "S01E10.mkv"])

    def test_times(self):
        for text, seconds in {"90": 90, "1:30": 90, "1:02:03": 3723, "1h2m3s": 3723,
                              "90s": 90, "2m": 120, " 1:00 ": 60, "0": 0}.items():
            self.assertEqual(s2k.seconds(text), seconds, text)
        for text in ("", "soon", "1h30", "1:2:3:4"):
            self.assertIsNone(s2k.seconds(text), text)

    def test_youtube_start(self):
        for url, seconds in {"https://youtu.be/jNQXAC9IVRw?t=90": 90,
                             "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=1m30s": 90,
                             "https://www.youtube.com/watch?v=jNQXAC9IVRw#t=45": 45,
                             "https://www.youtube.com/embed/jNQXAC9IVRw?start=12": 12,
                             "https://youtu.be/jNQXAC9IVRw": 0,
                             "https://example.com/v?t=90": 0}.items():
            self.assertEqual(s2k.youtube_start(url), seconds, url)

    def test_kinds_without_asking_the_network(self):
        for target, kind in {"a.mkv": "video", "a.MP4": "video", "a.flac": "audio",
                             "a.JPG": "image", "a.webp": "image", "noext": "video",
                             "a.m3u8": "video", "smb://nas/x/song.mp3": "audio",
                             "rtsp://cam/live": "video", "nfs://h/p/pic.jpeg": "image",
                             "https://host/v.mp4?token=1": "video",
                             "https://youtu.be/jNQXAC9IVRw": None}.items():
            self.assertEqual(s2k.kind_of(target), kind, target)

    def test_lines(self):
        self.assertEqual(s2k.lines("a\n\n # c\n#d\nfile:///x y\r\n\0b\0\n"),
                         ["a", "file:///x y", "b"])


class Folders(Case):
    def scan(self, folder):
        out = io.StringIO()
        with redirect_stdout(out):
            found = s2k.media_in(os.path.join(self.dir, folder))
        return [os.path.relpath(p, self.dir) for p in found], out.getvalue()

    def test_videos_in_natural_order_leaving_out_pictures(self):
        for p in ["Show/Season 10/E01.mkv", "Show/Season 2/E10.mkv", "Show/Season 2/E9.mkv",
                  "Show/poster.jpg", "Show/Season 2/E10-thumb.jpg", "Show/.hidden/x.mkv",
                  "Show/.y.mkv", "Show/notes.txt"]:
            self.file(p)
        found, said = self.scan("Show")
        self.assertEqual(found, ["Show/Season 2/E9.mkv", "Show/Season 2/E10.mkv",
                                 "Show/Season 10/E01.mkv"])
        self.assertIn("Show: 3 videos, leaving out 2 pictures", said)

    def test_pictures_when_nothing_else(self):
        for p in ["Pics/b10.png", "Pics/b2.jpg", "Pics/a.webp", "Pics/x.txt"]:
            self.file(p)
        found, said = self.scan("Pics")
        self.assertEqual(found, ["Pics/a.webp", "Pics/b2.jpg", "Pics/b10.png"])
        self.assertIn("Pics: 3 pictures", said)

    def test_no_media(self):
        self.file("Empty/readme.txt")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            s2k.media_in(os.path.join(self.dir, "Empty"))

    def test_subtitles_named_after_the_video(self):
        for p in ["S/E9.mkv", "S/E10.mkv", "S/E9.en.srt", "S/e9.ES.forced.srt",
                  "S/Subs/E9.fr.srt", "S/Subs/E10.de.ass", "S/E9.srt.bak", "S/Other.srt"]:
            self.file(p)
        found = [os.path.relpath(p, self.dir) for p in s2k.subtitles_for(os.path.join(self.dir, "S/E9.mkv"))]
        self.assertEqual(found, ["S/E9.en.srt", "S/e9.ES.forced.srt", "S/Subs/E9.fr.srt"])

    def test_expand(self):
        a, b = self.file("in/a b.mkv"), self.file("in/c.jpg")
        stdin, sys.stdin = sys.stdin, io.StringIO(f"{a}\n# no\nfile://{urllib.parse.quote(b)}\n")
        try:
            got = s2k.expand(["-", "https://host/v.mp4", "smb://nas/a.mkv"])
        finally:
            sys.stdin = stdin
        self.assertEqual(got, [a, b, "https://host/v.mp4", "smb://nas/a.mkv"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            s2k.expand([os.path.join(self.dir, "missing.mkv")])


class LocalFiles(Case):
    def test_video_with_subtitles_next_to_it(self):
        self.kodi.duration = 3
        title = "My Movie (2019) & Co+ é"
        video = self.file(f"m/{title}.mkv", VIDEO)
        for sub in [f"{title}.en.srt", f"{title.lower()}.ES.forced.srt", f"Subs/{title}.fr.ass"]:
            self.file(f"m/{sub}", SRT)
        self.file("m/Other.srt", SRT)
        self.assertSent(self.send(video), "Sent 1 item with 3 subtitle files", "Playing.")
        methods = self.kodi.methods()
        self.assertLess(methods.index("Playlist.Clear"), methods.index("Playlist.Add"))
        self.assertLess(methods.index("Playlist.Add"), methods.index("Player.Open"))
        (url,) = self.kodi.added(1)
        self.assertEqual(name(url), f"{title}.mkv")
        found = sorted(name(s) for s in self.kodi.found_subs[0][1])
        self.assertEqual(found, sorted([f"{title}.en.srt", f"{title.lower()}.ES.forced.srt",
                                        f"{title}.fr.ass"]))
        self.assertTrue(self.kodi.fetched(url, 206))

    def test_show_folder_queued(self):
        for p in ["Season 1/E1.mkv", "Season 1/E2.mkv", "Season 1/E10.mkv", "Season 2/E1.mkv"]:
            self.file(f"Show/{p}", VIDEO)
        self.file("Show/poster.jpg", JPEG)
        p = self.start("-q", os.path.join(self.dir, "Show"))
        self.assertTrue(wait_for(lambda: self.kodi.added(1)))
        time.sleep(12)
        self.assertIsNone(p.poll(), "stopped serving files still in the queue")
        with self.kodi.lock:
            self.kodi.playlists[1] = []
        self.assertSent(self.finish(p), "Show: 4 videos, leaving out 1 picture", "Queued 4 items.")
        self.assertEqual([name(f) for f in self.kodi.added(1)], ["E1.mkv", "E2.mkv", "E10.mkv", "E1.mkv"])
        self.assertNotIn("Player.Open", self.kodi.methods())

    def test_music_folder(self):
        self.kodi.duration = 2
        for song in ["1 one.mp3", "02 two.flac", "10 ten.ogg"]:
            self.file(f"Album/{song}", MP3)
        self.file("Album/cover.jpg", JPEG)
        self.assertSent(self.send(os.path.join(self.dir, "Album")), "Album: 3 songs, leaving out 1 picture")
        self.assertEqual([name(f) for f in self.kodi.added(0)], ["1 one.mp3", "02 two.flac", "10 ten.ogg"])
        self.assertEqual(self.kodi.calls_of("Player.Open")[0]["item"], {"playlistid": 0, "position": 0})
        self.assertEqual(len(self.kodi.history), 3)

    def test_songs_with_videos_go_to_the_video_player(self):
        self.kodi.duration = 2
        self.assertSent(self.send(self.file("mix/a.mp3", MP3), self.file("mix/b.mkv", VIDEO)))
        self.assertEqual(len(self.kodi.added(1)), 2)
        self.assertEqual(self.kodi.added(0), [])

    def test_gaps_between_items(self):
        self.kodi.duration = 4.6  # ends just before polls, now and then
        videos = [self.file(f"gap/{i}.mkv", VIDEO) for i in range(4)]
        self.assertSent(self.send(*videos, timeout=90))
        self.assertEqual(len(self.kodi.history), 4)
        for url in self.kodi.added(1):
            self.assertTrue(self.kodi.fetched(url, 206), f"not served: {url}")

    def test_start_time(self):
        self.kodi.duration = 4
        self.assertSent(self.send("--start", "1:02:03", self.file("st/v.mkv", VIDEO)))
        self.assertEqual(self.kodi.calls_of("Player.Seek")[0]["value"]["time"],
                         {"hours": 1, "minutes": 2, "seconds": 3})

    def test_subtitles_given_in_the_background(self):
        self.kodi.duration = 6
        sub = self.file("bg/other name.srt", SRT)
        out, _ = self.assertSent(self.send("--bg", "--sub", sub, self.file("bg/film.mkv", VIDEO), timeout=30),
                                 "in the background (pid", "Playing.")
        pid = int(out.split("(pid ")[1].split(")")[0])
        self.assertTrue(alive(pid), "the background process is gone already")
        (added,) = self.kodi.calls_of("Player.AddSubtitle")
        self.assertTrue(self.kodi.fetched(added["subtitle"], 200))
        self.assertTrue(wait_for(lambda: not alive(pid), 40), "the background process never exited")

    def test_stdin(self):
        a, b = self.file("in/a b.mkv", VIDEO), self.file("in/c.mkv", VIDEO)
        p = self.start("-q", "-", stdin=True)
        stdin, p.stdin = p.stdin, None  # or Python < 3.10's communicate() flushes it
        assert stdin is not None
        stdin.write(f"{a}\n\n# not this\nfile://{urllib.parse.quote(b)}\n")
        stdin.close()
        self.assertTrue(wait_for(lambda: self.kodi.added(1)))
        with self.kodi.lock:  # queued files are served until they leave the queue
            self.kodi.playlists[1] = []
        self.assertSent(self.finish(p), "Queued 2 items.")
        self.assertEqual([name(f) for f in self.kodi.added(1)], ["a b.mkv", "c.mkv"])

    def test_wifi_outage_while_serving(self):
        self.kodi.duration = 25
        p = self.start(self.file("out/v.mkv", VIDEO))
        self.assertTrue(wait_for(lambda: self.kodi.history))
        time.sleep(3)
        self.kodi.down_until = time.monotonic() + 14
        time.sleep(15)
        self.assertIsNone(p.poll(), "gave up during a short outage")
        with self.kodi.lock:
            self.kodi.player = None
        self.assertSent(self.finish(p))

    def test_serves_only_kodi(self):
        self.kodi.duration = 30
        video = self.file("sec/v.mkv", VIDEO)
        self.file("sec/v.en.srt", SRT)
        p = self.start(video)
        self.assertTrue(wait_for(lambda: self.kodi.added(1)))
        url = self.kodi.added(1)[0]
        folder = url.rsplit("/", 1)[0] + "/"

        def get(url, source="127.0.0.1"):
            u = urllib.parse.urlsplit(url)
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5, source_address=(source, 0))
            conn.request("GET", u.path)
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type") or "", r.read()

        status, kind, page = get(folder)
        self.assertEqual((status, kind.split(";")[0]), (200, "text/html"))
        self.assertIn(b'href="v.en.srt"', page)
        self.assertEqual(get(folder, "127.0.0.2")[0], 404)
        self.assertEqual(get(url, "127.0.0.2")[0], 404)
        token = folder.split("/")[3]
        self.assertEqual(get(folder.replace(token, "x" * len(token)))[0], 404)
        self.assertEqual(get(folder + "nope.mkv")[0], 404)
        self.assertEqual(get(folder + "..%2F..%2Fetc%2Fpasswd")[0], 404)
        self.assertEqual(get(folder + "../0/v.mkv")[0], 404)
        self.assertTrue(wait_for(lambda: self.kodi.history))
        time.sleep(3)
        with self.kodi.lock:
            self.kodi.player = None
        self.assertSent(self.finish(p))


class Pictures(Case):
    def close_viewer(self):
        time.sleep(3)  # until send2kodi has seen it
        with self.kodi.lock:
            self.kodi.viewer = False

    def test_folder_slideshow_keeps_pngs_and_order(self):
        self.file("Pics/a10.png", PNG)
        self.file("Pics/a2.jpg", JPEG)
        self.file("Pics/B1.webp", WEBP)
        p = self.start(os.path.join(self.dir, "Pics"))
        self.assertTrue(wait_for(lambda: self.kodi.viewer, 10))
        (opened,) = self.kodi.calls_of("Player.Open")
        self.assertIs(opened["item"]["recursive"], False)
        self.assertEqual(opened["options"], {"shuffled": False})
        self.assertEqual([name(s) for s in self.kodi.slides], ["1 a2.jpg", "2 a10.png", "3 B1.webp"])
        self.assertIs(self.kodi.slides_random, False)
        self.close_viewer()
        self.assertSent(self.finish(p), "Pics: 3 pictures", "Playing.")

    def test_single_png(self):
        p = self.start(self.file("one/shot.png", PNG))
        self.assertTrue(wait_for(lambda: self.kodi.viewer, 10))
        self.close_viewer()
        self.assertSent(self.finish(p), "Playing.")
        self.assertEqual(name(self.kodi.calls_of("Player.Open")[0]["item"]["file"]), "shot.png")

    def test_queued_pngs_are_dropped(self):
        self.assertSent(self.send("-q", self.file("q/a.png", PNG), self.file("q/b.jpg", JPEG)),
                        "accepted only 1 of 2 picture(s)", code=1)

    def test_queue_while_a_slideshow_shows(self):
        self.kodi.viewer, self.kodi.slides = True, ["http://x/y.jpg"]
        self.assertSent(self.send("-q", self.file("q/a.jpg", JPEG)), "already showing", code=1)

    def test_not_with_videos(self):
        self.assertSent(self.send(self.file("pv/a.jpg", JPEG), self.file("pv/b.mkv", VIDEO)),
                        "pictures can't be sent together", code=2)
        self.assertEqual(self.kodi.calls, [])

    def test_links(self):
        p = self.start(f"{web()}/img")  # no extension: downloaded and served from here
        self.assertTrue(wait_for(lambda: self.kodi.viewer, 10))
        self.assertTrue(self.kodi.calls_of("Player.Open")[0]["item"]["file"].endswith("/image0.png"))
        self.close_viewer()
        self.assertSent(self.finish(p))
        self.kodi.calls.clear()
        out, _ = self.assertSent(self.send(f"{web()}/pic.jpg"))  # given to Kodi as it is
        self.assertEqual(self.kodi.calls_of("Player.Open")[0]["item"]["file"], f"{web()}/pic.jpg")
        self.assertNotIn("Serving", out)
        p = self.start(f"{web()}/pic.jpg", f"{web()}/img")  # a slideshow: all downloaded
        self.assertTrue(wait_for(lambda: self.kodi.viewer and len(self.kodi.slides) == 2, 10))
        self.assertEqual([name(s) for s in self.kodi.slides], ["1 image0.jpg", "2 image1.png"])
        self.close_viewer()
        self.assertSent(self.finish(p))


class Links(Case):
    def test_media_links_play_directly(self):
        for url in ["clip.mp4", "stream", "live.m3u8", "hls", "nohead/x"]:
            self.kodi.calls.clear()
            out, _ = self.assertSent(self.send(f"{web()}/{url}"), "Playing.")
            self.assertEqual(self.kodi.added(1), [f"{web()}/{url}"])
            self.assertNotIn("Serving", out)
        self.kodi.calls.clear()
        self.assertSent(self.send(f"{web()}/song"))
        self.assertEqual(self.kodi.added(0), [f"{web()}/song"])

    def test_pages_and_youtube(self):
        self.assertSent(self.send(f"{web()}/page"))
        self.assertEqual(self.kodi.added(1), [f"plugin://plugin.video.sendtokodi/?{web()}/page"])
        self.kodi.calls.clear()
        self.assertSent(self.send("https://youtu.be/jNQXAC9IVRw?t=1m30s"))
        self.assertEqual(self.kodi.added(1), ["plugin://plugin.video.youtube/play/?video_id=jNQXAC9IVRw&seek=90"])
        self.assertEqual(self.kodi.calls_of("Player.Seek"), [])  # the add-on seeks
        self.kodi.calls.clear()
        self.assertSent(self.send("-s", "45", "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=90"))
        self.assertEqual(self.kodi.added(1), ["plugin://plugin.video.youtube/play/?video_id=jNQXAC9IVRw&seek=45"])
        self.kodi.calls.clear()
        self.assertSent(self.send("--ytdlp", "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=90"))
        self.assertEqual(self.kodi.added(1),
                         ["plugin://plugin.video.sendtokodi/?https://www.youtube.com/watch?v=jNQXAC9IVRw&t=90"])
        self.assertEqual(self.kodi.calls_of("Player.Seek")[0]["value"],
                         {"time": {"hours": 0, "minutes": 1, "seconds": 30}})
        self.kodi.calls.clear()
        self.kodi.addons = ["plugin.video.sendtokodi"]
        self.assertSent(self.send("-q", "https://youtu.be/jNQXAC9IVRw"))
        self.assertEqual(self.kodi.added(1), ["plugin://plugin.video.sendtokodi/?https://youtu.be/jNQXAC9IVRw"])
        self.kodi.addons = []
        self.assertSent(self.send(f"{web()}/page"), "SendToKodi add-on", code=1)

    def test_other_urls_go_to_kodi_as_they_are(self):
        self.assertSent(self.send("-q", "smb://nas/Movies/x.mkv", "nfs://nas/Music/y.flac"))
        self.assertEqual(self.kodi.added(1), ["smb://nas/Movies/x.mkv", "nfs://nas/Music/y.flac"])

    def test_subtitles_for_a_link(self):
        sub = self.file("subs/extra.es.srt", SRT)
        out, _ = self.assertSent(self.send("--sub", sub, f"{web()}/clip.mp4"))
        (added,) = self.kodi.calls_of("Player.AddSubtitle")
        self.assertEqual(name(added["subtitle"]), "extra.es.srt")
        self.assertTrue(self.kodi.fetched(added["subtitle"], 200))
        self.assertNotIn("Serving local file(s) until", out)

    def test_live_stream(self):
        self.kodi.live = True
        self.assertSent(self.send(f"{web()}/live.m3u8"), "Playing.")

    def test_unplayable_link(self):
        self.assertSent(self.send(f"{web()}/missing.mp4"),
                        "nothing started playing", "the link may be unsupported", code=1)


class Playback(Case):
    def test_a_player_stopping_between_two_calls(self):
        self.kodi.duration = 30
        self.kodi.fail_getprops = 2
        self.assertSent(self.send(f"{web()}/clip.mp4"), "Playing.")

    def test_seek_waits_for_the_first_frame(self):
        self.kodi.duration = 30
        self.kodi.open_delay = 3.0  # the first poll sees -0.1s
        self.kodi.neg_start = True
        self.assertSent(self.send("-s", "20", f"{web()}/clip.mp4"), "Playing.")
        player = self.kodi.player
        assert player is not None
        ((seconds, _, after_opening),) = player["seeks"]
        self.assertEqual(seconds, 20)
        self.assertTrue(after_opening)

    def test_old_player_while_an_addon_resolves(self):
        self.kodi.resolve_delay = 4
        self.kodi.duration = 1000
        with self.kodi.lock:
            self.kodi.playlists[1] = [{"file": "http://old.invalid/old.mkv"}]
            self.kodi.current_playlist, self.kodi.current_index = 1, 0
            self.kodi._start_player("video", 1, "http://old.invalid/old.mkv")["started"] -= 100
        self.assertSent(self.send("--start", "30", f"{web()}/page"))
        methods = self.kodi.methods()
        self.assertLess(methods.index("Player.Stop"), methods.index("Playlist.Clear"))
        player = self.kodi.player
        assert player is not None
        ((seconds, file, _),) = player["seeks"]
        self.assertEqual(seconds, 30)
        self.assertTrue(file.startswith("http://resolved.invalid/"), "seeked the old video")

    def test_stop_and_pause(self):
        with self.kodi.lock:
            self.kodi._start_player("video", 1, "x")
        self.assertSent(self.send("--pause"))
        self.assertEqual(self.kodi.calls_of("Player.PlayPause"), [{"playerid": 1}])
        self.assertSent(self.send("--stop"))
        self.assertEqual(self.kodi.calls_of("Player.Stop"), [{"playerid": 1}])


class Problems(Case):
    """What people get wrong, or run into, and what they're told."""

    def test_kodi_unreachable(self):
        self.assertSent(self.send(f"{web()}/clip.mp4", env={"KODI_PORT": "9"}), "cannot reach Kodi", code=1)

    def test_wrong_serve_ip(self):
        self.assertSent(self.send(self.file("v.mkv", VIDEO), env={"SERVE_IP": "192.0.2.1"}),
                        "cannot serve files on SERVE_IP=192.0.2.1", code=1)

    def test_settings_that_should_be_numbers(self):
        for key in ("KODI_PORT", "SERVE_PORT", "START_TIMEOUT"):
            self.assertSent(self.send(f"{web()}/clip.mp4", env={key: "x"}),
                            f"{key} has to be a number, not 'x'", code=1)

    def test_firewall_in_the_way(self):
        self.kodi.offline = True
        _, err = self.assertSent(self.send(self.file("v.mkv", VIDEO), env={"START_TIMEOUT": "5"}),
                                 "Kodi never reached this computer", code=1)
        self.assertIn("Is a firewall blocking port", err)

    def test_requests_from_another_address(self):
        self.kodi.source_ip = "127.0.0.2"
        self.assertSent(self.send(self.file("v.mkv", VIDEO), env={"START_TIMEOUT": "5"}),
                        "requests came from 127.0.0.2, but files are only served to "
                        "Kodi's address, 127.0.0.1", code=1)

    def test_a_file_kodi_cant_play(self):
        self.kodi.unplayable = True
        self.assertSent(self.send(self.file("v.mkv", VIDEO), env={"START_TIMEOUT": "5"}),
                        "Kodi reached this computer but did not start playing", code=1)

    def test_usage(self):
        video = self.file("v.mkv", VIDEO)
        for args, code, text in [
                (["-q", "--start", "10", video], 2, "--start and --sub"),
                (["--sub", video, self.file("p.jpg", JPEG)], 2, "--start and --sub"),
                (["--start", "soon", video], 2, "not a time"),
                (["--sub", "/nope.srt", video], 2, "no such file"),
                ([os.path.dirname(self.file("empty/readme.txt"))], 1, "no videos, songs or pictures"),
                (["/nonexistent.mkv"], 1, "not a URL, file or folder"),
                ([], 2, "nothing to send")]:
            self.assertSent(self.send(*args), text, code=code)


class Installer(Case):
    def install(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in s2k.DEFAULTS}
        env.update(HOME=self.dir, XDG_CONFIG_HOME=os.path.join(self.dir, "config"))
        r = subprocess.run(["bash", INSTALLER, *args], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def test_install_and_uninstall(self):
        bin_dir = os.path.join(self.dir, "bin")
        self.assertSent(self.install("--host", "127.0.0.1", "--port", str(self.kodi.port),
                                     "--bin-dir", bin_dir, "-y"), "Kodi is there.")
        self.assertTrue(os.access(os.path.join(bin_dir, "send2kodi"), os.X_OK))
        with open(os.path.join(self.dir, "config", "send2kodi", "config")) as f:
            config = f.read()
        self.assertIn("KODI_HOST=127.0.0.1\n", config)
        self.assertIn(f"KODI_PORT={self.kodi.port}\n", config)
        self.assertSent(self.install("--uninstall", "--bin-dir", bin_dir, "-y"), "removed", "kept")
        self.assertFalse(os.path.exists(os.path.join(bin_dir, "send2kodi")))

    def test_bad_arguments(self):
        self.assertSent(self.install("--host"), "--host needs a value.", code=1)
        self.assertSent(self.install("--host", "--yes"), "--host needs a value.", code=1)
        self.assertSent(self.install("--port", "http", "--host", "127.0.0.1", "-y"),
                        "the port has to be a number, not 'http'", code=1)


if __name__ == "__main__":
    unittest.main()
