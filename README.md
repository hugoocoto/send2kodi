# send2kodi

Send YouTube links, anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) can play, links to media files and streams, and the videos, music and pictures on your computer to Kodi, from the command line.

```sh
send2kodi https://youtu.be/jNQXAC9IVRw
send2kodi ~/Videos/holidays.mkv
send2kodi ~/Videos/Some.Show/
send2kodi ~/Pictures/beach/
```

It talks to Kodi's own JSON-RPC API, so there is nothing to install on the Kodi box beyond two common add-ons, and nothing on your computer beyond Python 3.

## How it works

| You send | Kodi plays it with |
|---|---|
| A YouTube video | the **YouTube** add-on, which starts in a few seconds, at the time in the link if it has one (`?t=1m30s`) |
| A link to a media file or stream (`.mp4`, `.mp3`, `.m3u8`, …, or any URL that serves video or audio) | Kodi itself, without any add-on |
| Any other URL (Vimeo, Twitch, a YouTube playlist, …) | the **SendToKodi** add-on, which resolves it with yt-dlp on the Kodi side |
| A file on your computer | a small built-in HTTP server on your computer, which Kodi streams from, subtitles included |
| A folder | its videos and songs, including those in its subfolders, in natural order (episode 2 before episode 10); or its pictures, if it has neither |
| Songs | Kodi's music player; with videos among them, the video player |
| Pictures: files, or image URLs, even ones without a `.jpg`/`.png` ending, like image-search proxy links | Kodi's picture viewer, where they stay on screen until you leave it; several make a slideshow. Pictures can't be mixed with videos or songs in one run. |

The local file server is deliberately narrow. It serves only the files you named, and the subtitles next to videos, under a random path that changes every run, answers only the Kodi box, and shuts itself down once Kodi has played them, or they have left the queue. Seeking works.

## Requirements

**Your computer:** Linux, Python 3.9 or newer. No packages. (macOS is untested.)

**Kodi:**

- *Settings → Services → Control → Allow remote control via HTTP* enabled.
- The [SendToKodi](https://github.com/firsttris/plugin.video.sendtokodi) add-on, for anything that is not YouTube.
- The YouTube add-on (in the official Kodi repository), for YouTube. Without it, YouTube links go through SendToKodi too, which works but is slower to start.

## Install

```sh
git clone https://github.com/hugoocoto/send2kodi
cd send2kodi
./install.sh
```

It asks for the Kodi box's address, checks that Kodi answers there and which add-ons it has, and installs:

| Path | What |
|---|---|
| `~/.local/bin/send2kodi` | the tool |
| `~/.config/send2kodi/config` | the address, plus any other setting you want as a default |

Non-interactively: `./install.sh --host 192.168.1.10 -y`. `./install.sh --uninstall` takes it back out.

Or skip the installer: the tool is a single self-contained script, and `--host` works without a config file.

```sh
install -Dm755 send2kodi ~/.local/bin/send2kodi
send2kodi --host 192.168.1.10 https://youtu.be/jNQXAC9IVRw
```

## Use

```sh
send2kodi URL_OR_FILE ...           # play now, replacing whatever is playing
send2kodi -q URL_OR_FILE ...        # add to the end of Kodi's queue
send2kodi -q -C                     # queue the URLs/paths in the clipboard
ls ~/Videos/*.mkv | send2kodi -     # read them from stdin, one per line
send2kodi -s 1:02:30 film.mkv       # start at 1h02m30s
send2kodi --sub film.es.srt URL     # with these subtitles
send2kodi --bg ~/Videos/film.mkv    # serve the file in the background
send2kodi --pause                   # toggle pause
send2kodi --stop
```

`-C` reads the clipboard with `wl-paste` on Wayland, or `xclip`/`xsel` on X11, one URL or path per line. Files copied in a file manager work too.

When playing now, `send2kodi` waits until Kodi really starts playing and exits with an error if it has not within a minute. Kodi accepts any link immediately, even one that will never play, so without this a bad link would fail silently on the TV. Live streams count as soon as they play, although they have no length.

`-s/--start` starts the first item at a time: `90`, `1:30`, `1:02:30` or `1h2m30s`. YouTube links start at the time in the link (`?t=…`) by themselves.

URLs that Kodi opens by itself, like `smb://`, `nfs://` or `rtsp://`, are passed on as they are.

### Folders

`send2kodi ~/Videos/Some.Show/` plays the videos in the folder and in the folders inside it, sorted the way people expect: episode 2 before episode 10, `Season 2/` before `Season 10/`. Music folders work the same, in Kodi's music player. Hidden files are skipped.

A folder with videos or songs sends only those, so posters and cover art don't get in the way; one with only pictures makes a slideshow. Each folder's line says what was found:

```
$ send2kodi ~/Pictures/trip
/home/you/Pictures/trip: 3 videos, leaving out 120 pictures
```

To show the pictures of such a folder instead, name them: `send2kodi ~/Pictures/trip/*.jpg`.

### Subtitles

Subtitle files named after a local video, next to it or in a `Subs` folder beside it (`film.srt`, `film.en.srt`, `Subs/film.es.forced.srt`, …), go along with it. Kodi finds them the same way it does next to its own files: they are in its subtitle menu, and turned on or not according to its subtitle language settings.

Any other subtitle file, for a local video or a URL, can be given with `--sub FILE`. Kodi loads it once playback starts, and shows it.

### Local files

Kodi streams the file from your computer, so the computer has to stay on and awake while it plays. By default `send2kodi` stays in the foreground until playback ends; press Ctrl-C to stop serving early.

With `--bg` (or `BACKGROUND=yes` in the config) it waits until Kodi has started playing, reports that, and then returns your prompt while a detached process keeps serving. You can close the terminal. The process exits by itself when playback ends.

Queued files (`-q`) are served for as long as they sit in Kodi's queue, however long that is. Clearing the queue or sending something new without `-q` releases them.

## Configuration

Settings come from, in increasing priority:

1. built-in defaults,
2. the config file, `~/.config/send2kodi/config` (or `--config PATH`),
3. environment variables of the same name,
4. command-line flags.

The config file is plain `KEY=value` lines:

| Setting | Default | Meaning |
|---|---|---|
| `KODI_HOST` | — | Address or hostname of the Kodi box. Required. Flag: `-H/--host` |
| `KODI_PORT` | `8080` | Kodi's web server port. Flag: `-p/--port` |
| `KODI_USER` | `kodi` | Web server login, if you set one in Kodi |
| `KODI_PASS` | *(empty)* | |
| `YOUTUBE` | `addon` | `addon` uses the YouTube add-on; `ytdlp` sends YouTube through SendToKodi too. Flag: `--ytdlp` |
| `BACKGROUND` | `no` | `yes` makes `--bg` the default. Flag `--fg` overrides it |
| `START_TIMEOUT` | `60` | Seconds to wait for playback before reporting failure |
| `SERVE_PORT` | `8765` | Port local files are served on. Any free port is used if it is taken |
| `SERVE_IP` | *(autodetected)* | Address Kodi reaches this computer on. Set it if autodetection picks the wrong interface, e.g. on a VPN |

To control more than one Kodi, keep a config per box and pick one with `--config`, or just pass `--host`.

## Troubleshooting

**"nothing started playing after 60s".** The add-on on Kodi could not resolve the link. yt-dlp breaks whenever sites change, so update the SendToKodi add-on first. Kodi's log (*Settings → System → Logging*) has the add-on's actual error.

**"rejected the login".** Kodi has a web server password set. Put it in `KODI_USER` / `KODI_PASS`.

**URLs work but local files don't.** Kodi cannot reach your computer. Check a firewall is not blocking `SERVE_PORT` (8765 by default), and that `SERVE_IP` (or the autodetected address) is one Kodi can reach.

**The subtitles next to a video don't show.** Their names have to start with the video's (minus its extension). Kodi lists them in its subtitle menu either way, but only turns one on by itself if *Settings → Player → Language → Preferred subtitle language* picks it.

**Security.** Kodi's web control has no password by default, which means anyone on your network can control it, with or without this tool. Setting one in Kodi is a good idea; `send2kodi` supports it.

## License

[MIT](LICENSE) © Hugo Coto
