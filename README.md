# send2kodi

Send YouTube links, anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) can play, and video files from your computer to Kodi, from the command line.

```sh
send2kodi https://youtu.be/jNQXAC9IVRw
send2kodi ~/Videos/holidays.mkv
```

It talks to Kodi's own JSON-RPC API, so there is nothing to install on the Kodi box beyond two common add-ons, and nothing on your computer beyond Python 3.

## How it works

| You send | Kodi plays it with |
|---|---|
| A YouTube video | the **YouTube** add-on, which starts in a few seconds |
| Any other URL (Vimeo, Twitch, a direct `.mp4`, a YouTube playlist, …) | the **SendToKodi** add-on, which resolves it with yt-dlp on the Kodi side |
| A file on your computer | a small built-in HTTP server on your computer, which Kodi streams from |

The local file server is deliberately narrow. It serves only the files you named, under a random path that changes every run, answers only the Kodi box, and shuts itself down once Kodi has played them, or they have left the queue. Seeking works.

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
send2kodi --bg ~/Videos/film.mkv    # serve the file in the background
send2kodi --pause                   # toggle pause
send2kodi --stop
```

When playing now, `send2kodi` waits until Kodi really starts playing and exits with an error if it has not within a minute. Kodi accepts any link immediately, even one that will never play, so without this a bad link would fail silently on the TV.

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

**Security.** Kodi's web control has no password by default, which means anyone on your network can control it, with or without this tool. Setting one in Kodi is a good idea; `send2kodi` supports it.

## License

[MIT](LICENSE) © Hugo Coto
