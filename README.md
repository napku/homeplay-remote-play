# HomePlay

Play the games on your gaming PC at home from your phone, a tablet or another computer, using only a web browser.

HomePlay runs on the gaming PC. It sends the picture and the sound to the browser, and the browser sends your keyboard, mouse, controller and touch input back to the PC. The graphics card does the video encoding (NVIDIA, AMD or Intel), so games keep running at full speed.

It automatically uses the best video codec your device can handle — **HEVC or AV1** where available (about half the data for the same quality, which really helps away from home), falling back to **H.264** everywhere else — and an **Auto** quality mode that matches the bitrate to your connection on its own. It also shares the clipboard both ways.

```
 Gaming PC (Windows)                                   Phone / work PC / laptop
 HomePlay + FFmpeg  ── picture + sound ──────────────▶  web browser
                    ◀───── keys, mouse, controller ──
                 (private Tailscale link, encrypted)
```

## What you need

| | |
|---|---|
| **Gaming PC** | Windows 10 or 11, signed in. Any graphics card. Without a supported one, the CPU encodes the video instead (slower). |
| **Python** | Version 3.9 or newer, from [python.org](https://www.python.org/downloads/). In the installer, tick **"Add python.exe to PATH"**. |
| **Tailscale** | A free account, from [tailscale.com](https://tailscale.com/download). You need it to play away from home. |
| **Browser** | Chrome or Edge (Android, Windows, Mac), or Safari on iOS 16.4 or newer. Firefox 130+ works on desktop. |

## 1. Set up the gaming PC (once)

1. Download the project (on GitHub: **Code → Download ZIP**) and unzip it somewhere permanent **inside your own user folder**, for example `C:\Users\<your name>\HomePlay`. Don't put it directly in `C:\`: other user accounts on the PC can usually change files there, and HomePlay holds the password that controls your PC (see [Security](#security)). Before you unzip, right-click the downloaded **`.zip`**, choose **Properties**, tick **Unblock** at the bottom, then **OK**. That stops Windows putting a warning on every file inside. (If you forget, see [Windows security warnings](#windows-security-warnings) below.)
2. Double-click **`start.bat`**. If a blue **"Windows protected your PC"** box appears, click **More info**, then **Run anyway** — this is expected for any new program that isn't from a big company, and is explained below. The first start takes a minute or two. During that first start it:
   - creates a private Python environment in the folder;
   - installs sound support;
   - installs controller support. If a **ViGEmBus** setup window opens, click through it. That driver creates the virtual Xbox controller;
   - installs **FFmpeg** with `winget`. Answer **Y** if it asks you to accept terms.
3. The window then shows your **password** and the addresses you can open:
   ```
   Password:   k7m2-qp9x-...
   This PC:    http://localhost:8080
   Anywhere:   https://gaming-pc.tail1234.ts.net   (via Tailscale)
   ```
4. Quick test on the PC itself: open `http://localhost:8080` in Chrome or Edge, enter the password and click **Connect**. You'll see your own screen inside the browser, a picture of a picture, so that's expected.

Keep the window open while you play. To stop HomePlay, close the window or press `Ctrl+C`.

## 2. Reach the PC from anywhere (once)

HomePlay doesn't open your PC to the internet. Instead, **Tailscale** creates a private, encrypted link between *your own* devices. It also gives the page a real `https://` address. Browsers only allow video decoding on secure pages.

1. Install **Tailscale** on the gaming PC and sign in.
2. Install the **Tailscale** app on your phone (App Store or Google Play) and sign in with the **same account**.
3. Close HomePlay and run `start.bat` again. It sets up `tailscale serve` automatically and prints an address like `https://gaming-pc.tail1234.ts.net`.
   - On the very first run, Tailscale may print a link asking you to **enable HTTPS** for your account. Open the link, click Enable, then run `start.bat` again.
4. On the phone: switch Tailscale on, open that address, enter the password, then tap **Connect**.

**iPhone/iPad tip:** in Safari, tap Share, then **Add to Home Screen**, and open HomePlay from the home screen. That gives you real full screen.

### The PC at work

- **Check your workplace's rules first.** Many employers don't allow games, remote-access tools or VPN apps on work computers, and work networks often block them. HomePlay makes no attempt to get around any of that. If it doesn't work on the work network, that's your IT department's decision.
- If you are allowed to install Tailscale on the work PC, it works exactly like the phone: open the `https://….ts.net` address in Chrome or Edge.
- If you can't install Tailscale there, use your phone or your own laptop instead. Tailscale *Funnel* (a public link) is **not** a good workaround. Funnel traffic is bandwidth-limited and far too slow for video. Opening a port on your router is possible, but it puts remote control of your PC on the internet behind a password (see *Advanced*).

## 3. Playing

**On a computer**
- **Click the picture** to capture the mouse ("Game mode"). Press **Esc** to free it.
- Open the menu with the **•••** button at the top, or with **Ctrl+Alt+Shift+M**.
- Click **Full screen** in the menu. In Chrome or Edge, keys like Esc, Alt+Tab and the Windows key then go to the game. **Hold Esc** to leave full screen.
- **Desktop mode** (in the menu) lets the mouse move freely. It's handier for game launchers and the Windows desktop.

**On a phone or tablet** (turn it sideways), choose one of three touch modes in the menu:
- **Trackpad:** drag to move the pointer and tap to click. Tap with two fingers for a right-click, and drag with two fingers to scroll. Hold, then drag, to drag something.
- **Tap to click:** tap exactly where you want to click. Hold for a right-click.
- **Controller:** an on-screen Xbox controller with sticks, D-pad, ABXY, bumpers, triggers, View and Menu.
- **Keyboard** (menu) opens the phone keyboard for typing in chat, logins and so on.
- **Real controllers** work in every mode, over Bluetooth or USB (Xbox, PlayStation and others). The PC sees them as Xbox controllers, and vibration works in Chrome.

**Stream quality** is set in the menu under Stream. Changes take about a second.

- **Auto** (recommended away from home): tick "Auto bitrate" and the PC adjusts the bitrate to your connection, backing off when it stutters and creeping back up when it clears. The bitrate you pick becomes the *maximum*.
- **Codec**: leave on "Auto" and HomePlay uses the best your device supports (HEVC/AV1 give a much better picture per megabit). Force **H.264** here if a device ever has trouble — though HomePlay already falls back on its own.
- Manual resolution / frame rate / bitrate if you'd rather set them yourself.

| Situation | Resolution | Frame rate | Bitrate (or Auto max) |
|---|---|---|---|
| Home Wi-Fi | Native or 1080p | 60 fps | 15–30 Mbit/s |
| Away, good internet at home | 1080p | 60 fps | 10–15 Mbit/s |
| Mobile data / weak Wi-Fi | 720p | 60 fps | 5–8 Mbit/s |

Away from home, your **home upload speed** limits the bitrate. Run a speed test at home and pick about 70 % of your upload as the Auto maximum. Turn on **Stats** in the menu to watch the frame rate, codec and dropped frames.

**Clipboard.** When the PC and browser are connected, the menu has **Clipboard** buttons: copy text on your phone and "Send clipboard to PC", or copy on the PC and it's offered to the device. Handy for pasting a password, a URL or a game code.

**Sound** is compressed with Opus by default: about 0.15 instead of 1.5 Mbit/s, which leaves more of your connection for the picture. On a fast home network you can tick **Uncompressed sound** in the menu to save another ~10 ms of sound delay.

## Making it as fast as possible

HomePlay already does the following on its own:

- **Picture:** uses the most efficient codec both sides support (AV1 or HEVC before H.264), and keeps every frame small enough that keyframes don't cause a hitch every couple of seconds on a slower connection.
- **Mouse:** in Chrome and Edge, mouse movement is sent the moment it happens instead of waiting for the next screen refresh, about half a frame sooner on average.
- **Sound:** Opus compression, plus a buffer that tunes itself. It settles around 25–35 ms on a steady connection and grows only if sound arrives late.
- **Under load:** HomePlay and FFmpeg run at above-normal priority, Windows isn't allowed to throttle them while a game has the focus, and sound capture uses Windows' multimedia scheduler.

What you can do on your side, most important first:

1. **Make sure Tailscale connects directly.** On a computer you play from, run `tailscale ping <your-pc-name>`. The first replies often say "via DERP(…)" while Tailscale sets up the connection; look at the **last** line. An IP address and port there (like `via 1.2.3.4:41641`) means a direct path. If it stays on "via DERP", your traffic is relayed through a Tailscale server, which adds delay. Tailscale's "connection types" guide explains how to fix that; it's often a router setting.
2. **Connect the gaming PC with a cable,** and use 5 GHz Wi-Fi on the phone or laptop.
3. **Use Chrome or Edge** on the device you play from, for the faster mouse path.
4. **Stream at 120 fps** if both screens can show it. Each frame then reaches you sooner (8 ms per frame instead of 16).
5. **Cap the game's frame rate** a little below what your graphics card can do (for example 120). A card stuck at 100 % makes the video encoder wait.
6. Turn on **Stats** in the menu: the round-trip time and "dropped" count show at once whether a change helped.

## Windows security warnings

HomePlay is a small, unsigned program that captures your screen and replays keyboard and mouse input. Those are exactly the things remote-control tools do — and antivirus watches for them, because malware does them too. So Windows may show a warning the first time. Here's what each one means and what to do.

**"Windows protected your PC" (blue box, SmartScreen).** This is the one you'll almost certainly see. It is **not** a virus finding. SmartScreen simply doesn't recognise a brand-new file that isn't signed by a known company, so it asks first. Click **More info**, then **Run anyway**. To avoid it for the whole folder at once, unblock the ZIP before you extract it: right-click the `.zip`, **Properties**, tick **Unblock**, **OK**, then extract.

**Windows Defender flags or removes a file.** Less common, but possible. If `start.bat` says a file is missing, or Defender pops up a threat alert, it's a false positive from the behaviour above. To fix it:
1. Open **Windows Security** → **Virus & threat protection** → **Protection history**, find the HomePlay item and choose **Allow** / **Restore**.
2. Right-click **`allow-in-defender.bat`** → **Run as administrator**. This adds *only the HomePlay folder* to Defender's exclusion list, so it stops scanning these files. It does **not** switch your antivirus off — the rest of your PC stays protected. The script shows you exactly what it will do and how to undo it.
3. Run `start.bat` again.

**Why I'm fairly sure it's a false positive.** HomePlay is built the way that trips antivirus the *least*: it's plain, readable Python (not a packed `.exe`, which is what gets flagged most), it runs in a normal visible window, it doesn't hide, it doesn't add itself to Windows startup, it doesn't touch the registry, and it doesn't download and run unknown programs. You can read every line of `homeplay_host.py` yourself.

**The only way to remove the warnings completely** is to sign the program with a code-signing certificate, which costs money each year (roughly €100+) and is meant for software companies — overkill for a personal tool. The unblock + exclusion steps above are the normal way to run your own tools.

> On a **work PC**, you very likely can't change Defender or add exclusions — your IT department controls it, and trying to get around that is their call, not something HomePlay will do for you. Use your phone or your own laptop instead.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Can't reach the PC" | Is `start.bat` running? Is Tailscale switched on, on **both** devices? Is the PC asleep? HomePlay keeps the PC awake while it runs, but it can't wake a PC that's already asleep. Turn off sleep in Windows power settings. |
| "Couldn't start the screen capture" | The PC is locked (Win+L) or a UAC prompt is showing. Windows doesn't let other programs see those screens. Sign in on the PC. To avoid this, let the screen turn off but don't lock the PC: Settings, Accounts, Sign-in options, "If you've been away…", set to **Never**. |
| A game ignores keyboard/mouse/controller | The game runs as administrator. Right-click `start.bat` and choose **Run as administrator**. Only do this if the HomePlay folder is in your own user folder: a program that runs as administrator from a folder other users can change lets them take over the PC. |
| No sound | Check the **Sound:** line in the HomePlay window. HomePlay records the PC's *default* playback device. |
| Controller does nothing | Check the **Gamepad:** line. Install the ViGEmBus driver ([releases page](https://github.com/nefarius/ViGEmBus/releases)), then restart HomePlay. |
| "This page has to be opened with https://" | Use the Tailscale address `https://….ts.net`, or `http://localhost:8080` on the PC itself. A plain `http://192.168…` address won't work. |
| Picture freezes now and then | Lower the bitrate or resolution. A freeze of up to two seconds means the connection dropped frames and is waiting for the next full picture. |
| Forgot the password | It's shown in the HomePlay window. For a new random one, run `start.bat --new-password`. To choose your own, run `start.bat --password "my secret phrase"`. |
| Several monitors | Pick the screen in the menu under Stream, or start with `--monitor 1`. |

Run `start.bat -v` for a detailed log.

## Advanced

Options (add them after `start.bat`):

| Option | Meaning |
|---|---|
| `--port 8080` | Local port. |
| `--fps 60`, `--bitrate 15` | Defaults, before the browser picks its own. |
| `--monitor 0` | Which screen to stream (0 = first). |
| `--encoder auto\|nvenc\|amf\|qsv\|x264` | Force a video encoder. |
| `--codec auto\|h264\|hevc\|av1` | Force a video codec (default: best the browser can decode). |
| `--auto` | Start in automatic-bitrate mode. |
| `--no-audio`, `--no-gamepad`, `--no-clipboard` | Turn those parts off. |
| `--lan` | Also accept devices on your home network directly. This uses HTTPS with a self-signed certificate: the browser shows a warning, and iPhones may refuse it. It needs `pip install cryptography`. |
| `--https --cert file.pem --key key.pem` | Serve HTTPS with your own certificate. |
| `--allow-host NAME` | Also accept web pages opened from this host name (repeat for more). Only needed if you reach HomePlay through your own domain or reverse proxy; `localhost` and your Tailscale address work without it. |
| `--demo` | Stream a test picture and only *log* input. Good for testing. |

**Port forwarding instead of Tailscale.** Not recommended. If you really want it, use `--lan`, forward that one TCP port on your router, and use a long password. Preferably also get a real certificate for your own domain (`--cert`/`--key`). Anyone who guesses the password controls your PC. After 6 wrong passwords, logins pause for 2 minutes.

**Start with Windows.** Press Win+R, type `shell:startup`, and put a shortcut to `start.bat` in that folder.

**Stop sharing over Tailscale.** Run `tailscale serve reset`.

**Files.** `homeplay_config.json` holds your password. `certs\` is only used with `--lan`. `allow-in-defender.bat` is the optional antivirus helper (see [Windows security warnings](#windows-security-warnings)). Everything else is the program. To uninstall, delete the folder and run `tailscale serve reset`; if you ran the Defender helper, also run `powershell Remove-MpPreference -ExclusionPath "<the HomePlay folder>"` as administrator.

## How it compares

HomePlay's strength is that the phone or work PC needs **only a browser**. If you can install apps, the mature free options are faster and more polished:

- **[Sunshine](https://github.com/LizardByte/Sunshine) + [Moonlight](https://moonlight-stream.org)**: the lowest latency, HDR, apps for every platform. Combine it with Tailscale the same way.
- **Steam Remote Play / Steam Link app**: easiest for Steam games.
- **Parsec**: polished, with a browser client too.

## Security

- Without `--lan`, HomePlay only listens on the PC itself (`127.0.0.1`). Only devices signed in to *your* Tailscale account can reach it, through Tailscale's encrypted link.
- A random password protects every connection. Six wrong tries pause logins for 2 minutes, for every device, and guesses sent at the same time count too. A password you choose with `--password` must have at least 10 characters.
- Web pages from other sites can't use your browser to reach HomePlay: a connection from a web page is refused unless the page was opened from HomePlay's own address (`localhost`, your Tailscale address, or a name you give with `--allow-host`).
- Only one device plays at a time. Connecting a second one disconnects the first.
- Sound compression talks to FFmpeg over a loopback-only socket; nothing extra is opened to your network.
- Text that a password manager marks as sensitive is not shared through the clipboard sync.
- Install HomePlay in your own user folder, not directly in `C:\`, and run it as administrator only when a game needs it. `homeplay_config.json` holds the password in plain text, and anyone who can change the folder can change the program.
- Tick **Remember on this device** only on devices that are yours: it keeps the password in that browser.

## License

MIT, see [LICENSE](LICENSE). You can use, change and share HomePlay freely.
