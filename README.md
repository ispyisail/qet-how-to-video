# qet-how-to-video

Short how-to videos of [QElectroTech](https://github.com/qelectrotech/qelectrotech-source-mirror)
features, made from a YAML story instead of by hand. It was written for
discussion [#1727](https://github.com/qelectrotech/qelectrotech-source-mirror/discussions/1727),
where scorpio810 asked for 1–3 minute videos of new features for the QET
YouTube channel.

A story says what a person would do ("open this menu, click here, type 40")
and what the saved project must contain afterwards. The tool runs the story
on a real QElectroTech build in a private virtual screen, checks the result,
and only then renders the video. If the story no longer does what it says
(a menu was renamed, the feature broke), you get an error instead of a video.

The user guide is on the QET wiki: [how_to_videos](https://github.com/qelectrotech/qelectrotech-source-mirror/wiki/how_to_videos).

## What you need

Linux, and:

| Need | Why |
|---|---|
| A **Qt 6** build of QElectroTech, and its source checkout | the video shows that binary; elements, fixtures and the logo come from the checkout |
| Qt 6 development files, `cmake`, `ninja` | to build the probe (once, automatically). **The same Qt 6 minor version** as the QElectroTech binary |
| `Xvfb`, `openbox`, `xdotool`, `xwininfo` (x11-utils), `scrot` | the private screen QElectroTech runs on |
| Python 3.9+, `PyYAML`, `Pillow` | the tool itself |
| Docker | ffmpeg runs from the `linuxserver/ffmpeg` image (`CLIP_FFMPEG_IMAGE` to change it) |
| Noto Sans fonts in `/usr/share/fonts/truetype/noto` | captions (`CLIP_FONTS`, `CLIP_FONT` to change them) |

On Debian or Ubuntu:

```bash
sudo apt install xvfb openbox xdotool x11-utils scrot python3-yaml python3-pil \
                 cmake ninja-build qt6-base-dev fonts-noto-core docker.io
```

## Quick start

```bash
git clone https://github.com/ispyisail/qet-how-to-video
cd qet-how-to-video
B=~/qelectrotech-source-mirror/build/qelectrotech     # your Qt 6 build
OUT=~/qet-videos                                       # big files: not /tmp

python3 probe.py --binary $B                                   # self-test, ~30 s
python3 story.py stories/add-a-fillet.yaml --binary $B --out $OUT --check-only   # ~45 s, no video
python3 story.py stories/add-a-fillet.yaml --binary $B --out $OUT                # ~2.5 min
python3 render.py $OUT add-a-fillet        # render again without recording again
```

If the binary is not at `<checkout>/<build dir>/qelectrotech`, add
`--qet-dir <checkout>`.

## What comes out

| File | For |
|---|---|
| `<name>.mp4` | YouTube: 1080p, captions burned in, generated music and click sounds at −14 LUFS, a soft English caption track |
| `<name>.github.mp4` | a PR or a discussion comment: 720p, under 10 MB |
| `<name>.en.srt` | captions |
| `<name>.description.txt` | summary, chapters, wiki and PR links, the QET commit, "music generated, no copyright" |
| `<name>.thumbnail.png` | 1280×720: title, the command's toolbar icon, the work before → after |
| `<name>.contact.png` | one frame every 5 s, to review the video at a glance |

Nothing is uploaded. Drag `<name>.github.mp4` into the PR or the #1727
discussion; give the `.mp4`, `.en.srt`, `.description.txt` and
`.thumbnail.png` to scorpio810, who publishes on the QET channel.

## Writing a story

Copy `stories/add-a-fillet.yaml`. Only `title` and `steps` are required.

```yaml
title: Add a fillet
summary: Round the corner between two drawn lines.
wiki: drawing_arcs          # QET wiki page, shown on the end card
pr: 1212                    # pull request, shown on the end card
fixture: tests/qttest/fixtures/coil_contact_wired.qet   # optional: beside the story, else relative to the checkout
thumbnail: {before: 2}      # before/after thumbnail: "before" = as chapter 2 starts

steps:
  - title: Add a fillet
    sub: Round the corner between two lines
  - chapter: "Pick the tool: {path:Add a fillet}"
  - menu: Add a fillet
  - click: [280, 300]
  - wait_for: {dialog: Fillet}
  - type: "40"
  - key: Return
  - wait_for: {undo: Add a fillet}

expect:
  - undo: Add a fillet
  - shapes: {type: Arc, count: 1}
```

Things are named, never measured: menus and toolbar buttons by their English
command text, dialogs by their title, widgets by class, object name or text,
points on the sheet in sheet coordinates. A probe library loaded into
QElectroTech (`LD_PRELOAD`, only when `QET_DEMO_PROBE_SOCKET` is set) finds
them on the screen.

### Steps

Each step has one main key; the others are options.

| Step | Does | Options |
|---|---|---|
| `title: Text` | a title card (the first and last become the intro and end cards) | `sub`, `hold` |
| `chapter: Text` | a caption that is also a YouTube chapter | `hold` |
| `caption: Text` / `end_caption:` | a caption until the next one | `hold` |
| `menu: Command` | walk the menu path to the command and click it | `pace` |
| `toolbar: Command` | click the command's toolbar button | `pace` |
| `click: [x, y]` | click a sheet point | `button: right`, `pace` |
| `click: {widget: {class:, name:, title:, text:, contains:}}` | click a widget; `text` also matches what a drop-down shows | |
| `click: {combo_item: Text}` | an entry of the drop-down list that is open | |
| `click: {toolbar: Command}` / `{item: {class:, n:, at:}}` | a toolbar button / the n-th sheet item | |
| `drag: {from: [x, y], to: [x, y]}` | drag on the sheet | |
| `move: [x, y]` or `{widget: …}` | move the pointer only | |
| `key: ctrl+z` | press keys, shown on screen | `show: false` |
| `press: ctrl+a` | press keys, not shown | |
| `type: "40"` | type text | |
| `wheel: [x, y]` | turn the mouse wheel | `notches`, `dir: up\|down`, `hold_key: alt` |
| `wait_for: {dialog: T}` | also `dialog_closed`, `undo`, `status`, `action` | `timeout` (s, default 10) |
| `highlight: {widget: …}` or `{rect: [x, y, w, h]}` | an orange frame | `secs`, `left` (fraction of a wide widget) |
| `frame: {rect: [x, y, w, h], fill: 0.7}` | zoom the sheet onto an area | |
| `hold: 2` | wait | |

In captions, `{path:Command}` becomes "Edit ▸ Add ▸ Command" and
`{tip:Command}` the command's status tip, both read from the running program.
`setup:` steps run before the recording starts.

### Checks (`expect:`)

After the last step the project is saved and checked. Any failure stops the
run before a video is made.

| Check | Passes when |
|---|---|
| `shapes: {type: Line, count: 2}` | the saved project has that many shapes of that type (`Line`, `Rectangle`, `Arc`, `Ellipse`…; no count = at least one) |
| `undo: Text` | an undo step with that name was pushed |
| `saved_text: 'regex'` | the regular expression is found in the saved project's files |
| `aligned: [left, top]` | every rectangle shares those edges |
| `view_zoom: 1.30` | the sheet ends at that zoom (130 %) |
| `arcs_tangent: true` | every arc end meets a line end tangentially |

A video that cannot fail is not evidence: give every story an `expect:`.

## Keeping videos current

[qet-verify](https://github.com/ispyisail/qet-verify) replays stories with
`--check-only` and fails when one no longer runs, so a pull request that
renames a command says which videos to record again:

```bash
./verify.sh --qet-dir <checkout> --binary <build>/qelectrotech --videos ../qet-how-to-video/stories
```

`stories/` holds stories for merged features, which pass on master.
`stories/pending/` holds stories for features still in an open pull request:
they run only on a build of that branch.

## For AI assistants

`AGENTS.md` has the rules in plain words for any assistant.
`skills/qet-how-to-video/SKILL.md` is the same as a Claude Code skill: copy
the folder into `~/.claude/skills/` (or `.claude/skills/` of a project).

## How it works

- **`probe/`, `probe.py`**: a small Qt library preloaded into an unmodified
  QElectroTech. It says where a command is (toolbar button, menu path), where
  widgets and sheet items are, converts sheet points to screen pixels, and
  sends events (command triggered, dialog shown or closed, undo step). Public
  Qt API only.
- **`story.py`**: the YAML runner. It waits on events, not on fixed sleeps.
- **`clip.py`**, **`shots.py`**: the private screen (own Xvfb, openbox, HOME,
  TMPDIR, no D-Bus, a copy of the binary) recorded at 2880×1620 with
  `QT_SCALE_FACTOR=1.5`, so the camera can zoom without blur.
- **`render.py`**, **`music.py`**: a virtual camera planned from where the
  clicks are, overlays drawn after the zoom, intro and end cards, music made
  from sine waves for each video, click and key sounds.

## Problems

| Symptom | Cause |
|---|---|
| `probe … did not answer` | the probe was built against another Qt 6 version than the binary uses |
| `'X' is in no menu` | the command was renamed or moved: update the story and record again |
| A story fails with `--speed 0.25` but passes at 0.5 | on builds before PR #1741 a quick second click at the end of a line opened its properties. A story that fails only when faster may have found a real bug: report it |
| A launch is answered by another QElectroTech | QET runs one instance per binary path and TMPDIR; the tool uses its own TMPDIR and a copy of the binary, so check nothing else (a container with host networking) is running QET the same way |
| `s6-applyuidgid: fatal` | the ffmpeg image's start-up wrapper refuses `-u`; the tool already passes `--entrypoint ffmpeg` |
| Captions out of step | the log and the video share the wall clock only with `-copyts`; `render.py` refuses a recording that is not stamped that way |
| Fewer than 3 chapters | YouTube needs 3 chapters of 10 s or more; `render.py` says so |
| `Cannot get the image data` at the end | seen as the session closes; the video is complete |

## Licence

GPL-2.0-or-later (`LICENSE`). `assets/openbox-rc.xml` is openbox's default
configuration (GPL) with two key bindings removed. The QElectroTech logo on
the cards is read from your QElectroTech checkout (`ico/256x256/qelectrotech.png`,
CC-BY-ND-3.0, see `ico/copyright` there) and is not part of this repository.
