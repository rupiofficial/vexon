# Vexon

Push-to-talk dictation for Windows that runs fully offline with local Whisper.

- **Hold `Ctrl + Win`**, speak, release: the text is typed into the focused app.
- **`Ctrl + Alt + Space`**: hands-free mode. Press it once to start and again to stop.
- A small status pill at the bottom of the screen shows the mic level while you speak.
- A tray icon gives you status, history, settings and quit.
- The app window is also a small **Chrome-style web browser**: tabs, address bar, back/forward/reload, bookmarks and browsing history. The dictation dashboard opens in a tab (see [Browser](#browser)).

## Install (Windows 10/11)
1. Install [Python 3.10+](https://www.python.org/downloads/). During setup, tick **"Add python.exe to PATH"**.
2. Download this repo: **Code → Download ZIP**, then extract it, or use `git clone`.
3. Double-click **`setup.bat`**. It installs everything, and if it finds an NVIDIA GPU it also installs the CUDA libraries (about 2 GB).
4. Double-click **`run.bat`** to start Vexon. The first launch downloads the Whisper model (about 480 MB).
5. Optional: run **`build.bat`** to make a standalone `dist\Vexon\Vexon.exe`, then pin that to your taskbar or Start Menu.

> If you also have Wispr Flow installed, quit it first or change Vexon's shortcut in Settings, because both use `Ctrl + Win` by default.

## Use
- The window opens on a **New Tab** page. The dictation dashboard has three pages: **Home** (stats and searchable history), **Dictionary** (vocabulary and replacements) and **Settings** (shortcuts, microphone, model, language, browser and behaviour). Open it with the microphone button in the toolbar, or type `vexon://app` in the address bar.
- Closing the window keeps Vexon running in the tray. To exit, choose **Quit** from the tray menu.
- `run.bat` runs the app from source with a console for logs. `build.bat` rebuilds the exe after you change the code.

## Browser
The window is a real Chromium browser (Microsoft Edge WebView2, which Windows 10/11 already includes), so ordinary websites, logins and video work.

- **Tabs:** `+` or `Ctrl+T` opens one, `Ctrl+W` closes it, `Ctrl+Shift+T` brings back a closed tab, `Ctrl+Tab` / `Ctrl+1..9` switch, and you can drag tabs to reorder them.
- **Address bar** (`Ctrl+L`): type a website (`github.com`, `localhost:3000`) or a search; suggestions come from your bookmarks and history. The search engine is set in the dashboard under Settings → Browser.
- **Back / Forward / Reload:** the toolbar buttons, `Alt+←`, `Alt+→`, `F5`. Reload turns into Stop while a page loads.
- **Bookmarks:** the star or `Ctrl+D`. They show in the bookmarks bar (`Ctrl+Shift+B` to hide it) and are managed at `vexon://bookmarks` (`Ctrl+Shift+O`).
- **History:** `vexon://history` (`Ctrl+H`), searchable, with delete and clear.
- **Google / Gmail:** the person button in the toolbar (and **Sign in** / **Gmail** on the New Tab page) takes you to Google's own sign-in page, so you enter your password only on Google's site. Vexon keeps the session in its browser profile, so you stay signed in between runs, and the button shows a green dot while a Google session exists. Bookmarks and history are not synced to your Google account.
- **Menu (⋮):** zoom, print, developer tools and the pages above.

Bookmarks and history are in `%APPDATA%\Vexon\browser.db`. Cookies, logins and the page cache are in `%LOCALAPPDATA%\Vexon\BrowserProfile`; delete that folder to sign out of everything.

## Code layout
- `app/engine.py`: hotkey, recording, Whisper, text cleanup, pasting and history
- `app/overlay.py`: the floating status pill
- `app/main.py`: the window (pywebview), tray icon, start-at-login and single-instance check
- `app/browser.py`: the browser: tabs (one WebView2 per tab), address-bar logic, bookmarks and history storage
- `app/ui/browser.html`, `keys.js`: the toolbar, tab strip and keyboard shortcuts
- `app/ui/newtab.html`, `history.html`, `bookmarks.html`, `pages.css`: the internal pages
- `app/ui/index.html`: the dictation dashboard
- `app/assets/brand/`: the Vexon logo (`vexon-logo.png` transparent, `vexon-logo-on-white.png`, `vexon-emblem.png`) and the app icon in `.ico`/`.png` sizes. The artwork is drawn for light backgrounds. `previous/` holds the old icon files.
- `tests/`: tests for the address-bar logic, bookmark/history storage and the VoiceType → Vexon data move

pywebview gives each window only one web view, so `browser.py` adds a WebView2 control per tab to the same window through pythonnet. Pages in `app/ui` are served to the tabs under a private address (`vexon.example`); only those pages can call the Python API, never a website.

## Settings
You can change everything on the Settings page, and changes apply immediately. The settings are stored in `%APPDATA%\Vexon\config.json`.

| key | meaning |
|---|---|
| `hotkey` | Hold-to-talk combo, e.g. `ctrl+windows`, `right ctrl`, `ctrl+shift` |
| `toggle_hotkey` | Hands-free start/stop combo |
| `model` | `tiny`, `base`, `small` (default), `medium`, `large-v3`, `distil-large-v3`, or `*.en` for English-only |
| `device` | `auto` (GPU if CUDA works, otherwise CPU), `cuda` or `cpu` |
| `language` | `null` to auto-detect, or a code like `"en"`, `"ta"` or `"hi"` |
| `vocabulary` | Names and jargon that Whisper should spell correctly |
| `replacements` | Spoken phrase → written text (`"new line": "\n"`) |
| `remove_fillers` | Removes um, uh and similar filler words |
| `search_engine` | Address-bar search: `google` (default), `duckduckgo`, `bing` or `brave` |
| `show_bookmarks_bar` | `true` or `false` |

Dictation history is saved in `%APPDATA%\Vexon\history.db` (SQLite).

## Tests
The address-bar and storage tests need no GUI. After `setup.bat`, run:

```
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Tips
- **GPU:** an NVIDIA card with 4 GB or more runs even `large-v3-turbo` in under a second per clip. On a CPU, use `base` or `small`.
- **Non-English languages:** pick the language and a larger model (`large-v3-turbo`) in Settings for much better accuracy.

## How it works (clean-room design)
These are the parts of a dictation tool like Wispr Flow, as seen by analyzing the shipped app with REA. Each part was then written from scratch:

| Component | Observed in Wispr Flow | Vexon |
|---|---|---|
| Global hotkey | Native helper with a low-level keyboard hook (`SetWindowsHookEx`) | `keyboard` lib (same WH_KEYBOARD_LL hook) |
| Audio capture | Renderer `getUserMedia` + AudioWorklet, Opus encoding | `sounddevice` 16 kHz mono |
| Speech-to-text | Streamed over WebSocket to their cloud API | Local `faster-whisper` (GPU/CPU) |
| Text insertion | Clipboard + `SendInput` paste | Clipboard + Ctrl+V, then the old clipboard is restored |
| Context reading | UI Automation (TextPattern/ValuePattern) | Not used (only the window title is logged) |
| Local data | SQLite + migrations (history, dictionary, notes) | SQLite history |

Vexon contains no code, assets or API access from Wispr Flow. It is an independent project and is not affiliated with Wispr.

## License
[MIT](LICENSE)
