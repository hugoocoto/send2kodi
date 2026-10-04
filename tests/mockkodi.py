"""A stand-in for Kodi's JSON-RPC API, close enough to test send2kodi with.

It behaves the way Kodi 21 (Omega) does, as far as send2kodi can tell:

- Playlist.Add and Playlist.Clear on the video and music playlists, and
  Player.Open on them, are queued for Kodi's main thread: they happen a
  moment later, in order.
- Adding to the picture playlist reads each picture's EXIF data there and
  then, and drops the pictures it can't read any from (all but JPEGs).
- Playing an http:// item starts by asking for its type (HEAD). A plugin://
  item is resolved while the old player keeps playing, with the playlist's
  position already moved to the new item.
- The subtitles of an http:// video on the LAN are looked for by listing its
  folder, parsed as CHTTPDirectory parses a web server's directory listing,
  and matching file names by prefix.
- Player.Open {"path": folder} lists the folder for a slideshow and sorts
  the pictures by name.
- A new player's clock reads 0 while it opens; -0.1s with neg_start, as
  Kodi's does before the first frame.

Attributes set after creating one (duration, live, offline, ...) make it
misbehave the ways a real one can.
"""

import html
import http.client
import http.server
import json
import queue
import re
import threading
import time
import urllib.parse

SUB_EXTS = (".utf .utf8 .utf-8 .sub .srt .smi .rt .txt .ssa .text .aqt .jss .ass "
            ".vtt .idx .ifo .zip .sup").split()
PIC_EXTS = (".png .jpg .jpeg .bmp .gif .ico .tif .tiff .tga .pcx .cbz .zip .rss "
            ".webp .jp2 .apng .avif").split()
MUSIC_EXTS = (".nsv .m4a .flac .aac .strm .pls .rm .rma .mpa .wav .wma .ogg .mp3 "
              ".mp2 .m3u .ac3 .dts .cue .aif .aiff .ape .mpc .wv .oga .tta .mka .tak "
              ".opus .dff .dsf .m4b").split()
VIDEO_EXTS = (".m4v .3g2 .3gp .nsv .tp .ts .ty .strm .pls .rm .rmvb .mpd .m3u "
              ".m3u8 .ifo .mov .qt .divx .xvid .vob .wmv .asf .ogm .m2v .avi .mpg "
              ".mpeg .mp4 .mkv .mk3d .dv .flv .mts .m2t .m2ts .evo .ogv .webm .wtv "
              ".trp .f4v").split()
KNOWN_EXTS = set(SUB_EXTS + PIC_EXTS + MUSIC_EXTS + VIDEO_EXTS + [".py", ".xml"])
SUB_DIRS = {"subs", "subtitles", "vobsubs", "sub", "vobsub", "subtitle"}
# CHTTPDirectory's, compiled as CRegExp does: caseless, DOTALL.
LISTED = re.compile(r'<a href="([^"]*)"[^>]*>\s*(.*?)\s*</a>(.+?)(?=<a|</tr|$)',
                    re.I | re.S)
FAILED = {"code": -32100, "message": "Failed to execute method."}


def kodi_decode(s):
    """CURL::Decode, which takes + for a space."""
    return urllib.parse.unquote_plus(s)


def ext_of(url):
    name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


def without_ext(name):
    """URIUtils::RemoveExtension: only extensions Kodi knows go."""
    i = name.rfind(".")
    return name[:i] if i >= 0 and name[i:].lower() in KNOWN_EXTS else name


def by_label(label):
    """StringUtils::AlphaNumericCompare, near enough."""
    return [int(p) if i % 2 else p.lower() for i, p in enumerate(re.split(r"(\d+)", label))]


def time_object(seconds):
    ms = int(round(max(0.0, seconds) * 1000))
    return {"hours": ms // 3600000, "minutes": ms // 60000 % 60,
            "seconds": ms // 1000 % 60, "milliseconds": ms % 1000}


class MockKodi:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []      # (time, method, params) of every JSON-RPC call
        self.fetches = []    # (time, method, url, status, type, size, range)
        self.history = []    # files that started playing
        self.found_subs = []  # (video, subtitles found next to it)
        self.playlists = {0: [], 1: [], 2: []}
        self.current_playlist, self.current_index = -1, -1
        self.player = None
        self.slides, self.slide_index, self.viewer = [], 0, False
        self.slides_random = None
        # Knobs.
        self.addons = ["plugin.video.sendtokodi", "plugin.video.youtube"]
        self.duration = 5.0       # of every item
        self.live = False         # streams with no length
        self.open_delay = 0.3     # until the first frame
        self.neg_start = False
        self.resolve_delay = 0.0  # of plugin:// items
        self.fail_getprops = 0    # Player.GetProperties calls to fail
        self.down_until = 0.0     # drop JSON-RPC connections until then
        self.offline = False      # can't reach anything: a firewall
        self.source_ip: "str | None" = None  # fetch from this address instead
        self.unplayable = False   # read files, but play none

        self.queue = queue.Queue()  # Kodi's main thread's messages
        threading.Thread(target=self._main_thread, daemon=True).start()
        threading.Thread(target=self._clock, daemon=True).start()
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                if time.monotonic() < mock.down_until:
                    self.close_connection = True
                    self.connection.close()  # looks like a network failure
                    return
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result = mock.handle(req["method"], req.get("params", {}))
                body = {"jsonrpc": "2.0", "id": req.get("id")}
                if isinstance(result, dict) and "error" in result:
                    body["error"] = result["error"]
                else:
                    body["result"] = result
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------ fetching --

    def fetch(self, url, method="GET", rng=None, limit=4096):
        """(status, headers, body) as Kodi gets them; (None, None, b"") when
        it can't connect."""
        u = urllib.parse.urlsplit(url)
        status, headers, data = None, None, b""
        if not self.offline:
            source = (self.source_ip, 0) if self.source_ip else None
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5,
                                              source_address=source)
            try:
                conn.request(method, u.path + (f"?{u.query}" if u.query else ""),
                             headers={"User-Agent": "Kodi/21.3 (mock)",
                                      **({"Range": rng} if rng else {})})
                r = conn.getresponse()
                status, headers = r.status, r.headers
                data = r.read(limit) if method == "GET" else b""
            except OSError as e:
                status = str(e)
            finally:
                conn.close()
        with self.lock:
            self.fetches.append((time.monotonic(), method, url, status,
                                 headers and headers.get("Content-Type"), len(data), rng))
        return (status if isinstance(status, int) else None), headers, data

    def list_folder(self, url, extensions):
        """CHTTPDirectory::GetDirectory, then CDirectory's extension filter."""
        status, headers, data = self.fetch(url, limit=1 << 20)
        if (status != 200 or headers is None
                or not (headers.get("Content-Type") or "").startswith("text/html")):
            return []
        page = data.decode("utf-8", "replace")
        u = urllib.parse.urlsplit(url)
        items, pos = [], 0
        while True:
            m = LISTED.search(page, pos)
            if not m:
                return items
            pos = m.end()
            link, name = m.group(1).lstrip("/"), html.unescape(m.group(2).strip()).rstrip("/")
            link = html.unescape(link.split("?", 1)[0].split("#", 1)[0])
            link = re.sub(r"[+;]", lambda c: "%%%x" % ord(c.group()), link)
            decoded = kodi_decode(link.rstrip("/"))
            if decoded not in ("..", "") and name == decoded:
                path = f"{u.scheme}://{u.netloc}{u.path}{link}"
                folder = path.endswith("/")
                if folder or ext_of(path) in extensions:
                    items.append({"label": name, "path": path, "folder": folder})

    def subtitles(self, video):
        """CUtil::ScanForExternalSubtitles, for an http:// video on the LAN."""
        name = urllib.parse.urlsplit(video).path.rsplit("/", 1)[-1]
        stem = name[:name.rfind(".")] if "." in name else name
        items = self.list_folder(video[:video.rfind("/") + 1], SUB_EXTS)
        for item in list(items):
            if item["folder"] and item["label"].lower() in SUB_DIRS:
                items += self.list_folder(item["path"], SUB_EXTS)
        found = []
        for item in items:
            candidate = without_ext(item["path"].rsplit("/", 1)[-1]).lower()
            if not item["folder"] and (candidate.startswith(stem.lower())
                                       or candidate.startswith(kodi_decode(stem).lower())):
                found.append(item["path"])
        return found

    # ---------------------------------------------------------- main thread --

    def _main_thread(self):
        while True:
            message, *args = self.queue.get()
            with self.lock:
                if message == "clear":
                    self.playlists[args[0]] = []
                elif message == "add":
                    self.playlists[args[0]] += args[1]
            if message == "next":
                time.sleep(0.4)  # Kodi has no player for a moment
            if message in ("play", "next"):
                self._play(*args)

    def _play(self, playlist, position):
        with self.lock:
            if position >= len(self.playlists[playlist]):
                return
            file = self.playlists[playlist][position]["file"]
            self.current_playlist, self.current_index = playlist, position
        if file.startswith("plugin://"):
            time.sleep(self.resolve_delay)  # the old player keeps playing
            seek = urllib.parse.parse_qs(urllib.parse.urlsplit(file).query).get("seek")
            with self.lock:
                player = self._start_player("video", playlist,
                                            f"http://resolved.invalid/{abs(hash(file))}.mp4")
                player["addon_seek"] = float(seek[0]) if seek else None
            return
        if not file.startswith("http"):  # smb://, rtsp://, ...
            with self.lock:
                self._start_player("audio" if ext_of(file) in MUSIC_EXTS else "video", playlist, file)
            return
        self.fetch(file, "HEAD")  # FillInMimeType
        status, headers, _ = self.fetch(file, rng="bytes=0-")
        if status not in (200, 206) or headers is None:  # skipped as unplayable
            with self.lock:
                self.player = None
            self._play(playlist, position + 1)
            return
        if self.unplayable:
            with self.lock:
                self.player = None
            return
        kind = (headers.get("Content-Type") or "").split("/")[0]
        audio = kind == "audio" or kind != "video" and ext_of(file) in MUSIC_EXTS
        subs = []
        if not audio:
            subs = [s for s in self.subtitles(file) if self.fetch(s)[0] == 200]
        with self.lock:
            self._start_player("audio" if audio else "video", playlist, file)["subtitles"] = subs
            self.found_subs.append((file, subs))

    def _start_player(self, kind, playlist, file):
        """Called with the lock held; returns the new player."""
        opened = time.monotonic() + self.open_delay
        self.player = {"type": kind, "playlistid": playlist, "file": file,
                       "opened": opened, "started": opened, "subtitles": [],
                       "added_subs": [], "seeks": []}
        self.history.append(file)
        return self.player

    def _clock(self):
        while True:
            time.sleep(0.05)
            with self.lock:
                p = self.player
                if p and not self.live and time.monotonic() - p["started"] >= self.duration:
                    self.player = None
                    if self.current_index + 1 < len(self.playlists[self.current_playlist]):
                        self.queue.put(("next", self.current_playlist, self.current_index + 1))

    # ------------------------------------------------------------- JSON-RPC --

    def handle(self, method, params):
        with self.lock:
            self.calls.append((time.monotonic(), method, json.loads(json.dumps(params))))
        handler = getattr(self, method.replace(".", "_"), None)
        if handler is None:
            return {"error": {"code": -32601, "message": "Method not found."}}
        return handler(params)

    def active(self):
        out = []
        if self.player:
            audio = self.player["type"] == "audio"
            out.append({"playerid": 0 if audio else 1, "playertype": "internal",
                        "type": self.player["type"]})
        if self.viewer:
            out.append({"playerid": 2, "playertype": "internal", "type": "picture"})
        return out

    def player_type(self, playerid):
        """The type of the active player with that id, or None."""
        return next((p["type"] for p in self.active() if p["playerid"] == playerid), None)

    def JSONRPC_Ping(self, params):
        return "pong"

    def Addons_GetAddons(self, params):
        return {"addons": [{"addonid": a, "type": "xbmc.python.pluginsource"} for a in self.addons],
                "limits": {"start": 0, "end": len(self.addons), "total": len(self.addons)}}

    def Player_GetActivePlayers(self, params):
        with self.lock:
            return self.active()

    def Player_GetProperties(self, params):
        with self.lock:
            if self.fail_getprops:
                self.fail_getprops -= 1
                return {"error": FAILED}
            kind, p, now = self.player_type(params["playerid"]), self.player, time.monotonic()
            if kind == "picture":
                return {"playlistid": 2, "position": self.slide_index,
                        "time": time_object(0), "totaltime": time_object(0)}
            if kind is None or p is None:
                return {"error": FAILED}
            opening = now < p["opened"]
            if opening and self.neg_start:
                clock = {"hours": 0, "minutes": 0, "seconds": 0, "milliseconds": -100}
            else:
                clock = time_object(0 if opening else now - p["started"])
            values = {
                "playlistid": self.current_playlist,
                "position": self.current_index if self.current_playlist == p["playlistid"] else -1,
                "time": clock,
                "totaltime": time_object(0 if opening or self.live else self.duration),
                "subtitles": [{"index": i, "name": s}
                              for i, s in enumerate(p["subtitles"] + p["added_subs"])],
            }
            return {k: values.get(k) for k in params["properties"]}

    def Player_GetItem(self, params):
        with self.lock:
            kind, p = self.player_type(params["playerid"]), self.player
            if kind == "picture":
                file = self.slides[self.slide_index] if self.slides else ""
                return {"item": {"file": file, "type": "picture", "label": file.rsplit("/", 1)[-1]}}
            if kind is None or p is None:
                return {"error": FAILED}
            return {"item": {"file": p["file"], "type": "unknown", "label": ""}}

    def Player_Open(self, params):
        item, options = params["item"], params.get("options", {})
        if "playlistid" in item:
            if item["playlistid"] != 2:
                self.queue.put(("play", item["playlistid"], item.get("position", 0)))
                return "OK"
            with self.lock:
                if not self.slides:
                    return "OK"
                self.slide_index, self.viewer = item.get("position", 0), True
            self.fetch(self.slides[self.slide_index])
            return "OK"
        if "path" in item:
            shuffled = options.get("shuffled")
            random = shuffled if isinstance(shuffled, bool) else item.get("random", True)
            items = sorted((i for i in self.list_folder(item["path"], PIC_EXTS) if not i["folder"]),
                           key=lambda i: by_label(i["label"]))
            for i in items:
                self.fetch(i["path"], "HEAD")  # FillInMimeType
            with self.lock:
                if self.player and self.player["type"] == "video":
                    self.player = None  # a slideshow stops videos
                self.slides, self.slides_random = [i["path"] for i in items], random
                self.slide_index, self.viewer = 0, bool(items)
            if items:
                self.fetch(items[0]["path"])  # shown
            return "OK"
        if "file" in item:
            file = item["file"]
            if ext_of(file) in PIC_EXTS:
                self.fetch(file)  # its EXIF data, if any
                with self.lock:
                    self.slides, self.slide_index, self.viewer = [file], 0, True
                self.fetch(file)  # shown
                return "OK"
            with self.lock:
                self.playlists[1] = [{"file": file}]
            self.queue.put(("play", 1, 0))
            return "OK"
        return {"error": {"code": -32602, "message": "Invalid params."}}

    def Player_Stop(self, params):
        with self.lock:
            kind = self.player_type(params["playerid"])
            if kind is None:
                return {"error": FAILED}
            if kind == "picture":
                self.viewer = False
            else:
                self.player = None
        return "OK"

    def Player_PlayPause(self, params):
        return {"speed": 0}

    def Player_Seek(self, params):
        with self.lock:
            p = self.player
            if self.player_type(params["playerid"]) not in ("video", "audio") or p is None:
                return {"error": FAILED}
            t = params["value"]["time"]
            s = t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)
            p["seeks"].append((s, p["file"], time.monotonic() >= p["opened"]))
            p["started"] = time.monotonic() - s
            return {"percentage": 0, "time": time_object(s), "totaltime": time_object(self.duration)}

    def Player_AddSubtitle(self, params):
        with self.lock:
            player = self.player
            if self.player_type(params["playerid"]) != "video" or player is None:
                return {"error": FAILED}

        def load():  # the player reads it a moment later
            time.sleep(0.5)
            if self.fetch(params["subtitle"])[0] == 200:
                with self.lock:
                    player["added_subs"].append(params["subtitle"])
        threading.Thread(target=load, daemon=True).start()
        return "OK"

    def Playlist_Clear(self, params):
        if params["playlistid"] == 2:
            with self.lock:
                self.viewer, self.slides = False, []
        else:
            self.queue.put(("clear", params["playlistid"]))
        return "OK"

    def Playlist_Add(self, params):
        items = params["item"] if isinstance(params["item"], list) else [params["item"]]
        if params["playlistid"] != 2:
            self.queue.put(("add", params["playlistid"], [{"file": i["file"]} for i in items]))
            return "OK"
        for i in items:
            status, _, data = self.fetch(i["file"])
            if status == 200 and data[:3] == b"\xff\xd8\xff":  # process_jpeg
                with self.lock:
                    self.slides.append(i["file"])
        return "OK"

    def Playlist_GetItems(self, params):
        with self.lock:
            if params["playlistid"] == 2:
                files = list(self.slides)
            else:
                files = [i["file"] for i in self.playlists[params["playlistid"]]]
        wanted = "file" in params.get("properties", [])
        items = [{"label": "", "type": "unknown", **({"file": f} if wanted else {})} for f in files]
        return {"items": items, "limits": {"start": 0, "end": len(items), "total": len(items)}}

    # ------------------------------------------------------- for the tests --

    def calls_of(self, method):
        with self.lock:
            return [c[2] for c in self.calls if c[1] == method]

    def methods(self):
        with self.lock:
            return [c[1] for c in self.calls]

    def added(self, playlist):
        """Every file added to a playlist, in order."""
        out = []
        for c in self.calls_of("Playlist.Add"):
            if c["playlistid"] == playlist:
                out += [i["file"] for i in (c["item"] if isinstance(c["item"], list) else [c["item"]])]
        return out

    def fetched(self, url=None, status=None):
        with self.lock:
            return [f for f in self.fetches
                    if (url is None or f[2] == url) and (status is None or f[3] == status)]
