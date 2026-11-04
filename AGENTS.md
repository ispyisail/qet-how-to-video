# Instructions for AI assistants

These are the rules for an AI assistant (Copilot, ChatGPT, Claude or any
other) asked to make a QElectroTech how-to video with this tool. Claude Code
users can install the same rules as a skill: `skills/qet-how-to-video/`.

## What the tool does

`story.py` runs a YAML story on a real QElectroTech build in a private
virtual screen, saves the project, checks the `expect:` block, and only then
renders the video. `README.md` has the full list of steps and checks.

## Rules

1. **Never upload or post anything** unless the person asks. The person
   uploads `<name>.github.mp4` to GitHub themselves; the YouTube pack goes to
   scorpio810.
2. **Name things, never measure them.** Menus and toolbar buttons by their
   English command text, dialogs by title, widgets by class, object name or
   text, sheet points in sheet coordinates. No pixel positions.
3. **Take every label from the QElectroTech source** (`tr("…")` in the C++,
   `<string>` in `.ui` files), not from memory or from a translation. The
   interface's source language is English.
4. **Every story has an `expect:` block** that would fail if the feature did
   not work. A video that cannot fail proves nothing.
5. **Run `--check-only` until it prints PASS** before recording. If a story
   fails only when run faster, report it as a possible QElectroTech bug; do
   not slow the story down to hide it.
6. **Stories for merged features go in `stories/`** and must pass on master.
   Stories for open pull requests go in `stories/pending/`.
7. **Look at the output before calling it done**: `<name>.contact.png` and a
   few full-size frames. Say plainly what you did not check (for example, that
   nobody listened to the audio).
8. Write captions in plain English: short sentences, the words the interface
   uses.

## Commands

```bash
python3 probe.py --binary <qet>                                          # self-test
python3 story.py stories/<feature>.yaml --binary <qet> --out <dir> --check-only
python3 story.py stories/<feature>.yaml --binary <qet> --out <dir>      # record + render
python3 render.py <dir> <feature>                                         # render again
```

Add `--qet-dir <checkout>` when the binary is not inside the checkout.
