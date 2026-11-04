---
name: qet-how-to-video
description: Make a short how-to video of a QElectroTech feature from a YAML story — recorded from the real GUI, checked against the saved project, with camera moves, English captions, generated music, and a YouTube pack (mp4, srt, description, thumbnail). Load when asked for a feature video, a demo, a how-to for a QET pull request, or discussion #1727 (scorpio810's call for 1–3 minute videos), or when a recorded story stops working after an interface change.
---

# How-to videos for QElectroTech

The tool is this repository (`qet-how-to-video`); its README has the full
step and check reference and the known problems. A story is
`stories/<feature>.yaml`; one command checks it, records it and renders it.

## Never upload

The person drags `<name>.github.mp4` (under 10 MB) into the PR or the #1727
discussion. The YouTube pack (`<name>.mp4`, `.en.srt`, `.description.txt`,
`.thumbnail.png`) goes to scorpio810, who publishes on the QET channel. Never
post or upload anything without being asked.

## Steps

1. **Check nothing else is running QElectroTech the same way** (for example a
   Docker container with host networking): QET hands a second launch to the
   first one. The tool uses its own screen, HOME, TMPDIR and a copy of the
   binary.
2. **Build the binary the video is about**: a Qt 6 build of master, or of the
   pull request's branch. The video shows whatever binary it is given; its
   commit goes on the end card.
3. **Draft the story** from the pull request and its wiki page: copy
   `stories/add-a-fillet.yaml`. Show it the way a person would: the menu path
   first (`menu: <command text>`, found by the probe, never pixels), then the
   toolbar button. Take every label from the source (`tr("…")`), not from
   memory; the interface is English. Sheet points are sheet coordinates.
   Always add an `expect:` block (shape counts, `undo:`, `saved_text:`…). A
   video that cannot fail is not evidence.
4. **Check only**: `python3 story.py <story> --binary <qet> --out <dir> --check-only`
   (about 45 s). Fix the story until it prints PASS. A story that fails only
   at a faster `--speed` may have found a real QElectroTech bug: say so, with
   the story, instead of slowing the story down to hide it.
5. **Record and render**: the same command without `--check-only`, about
   2.5 minutes. Put `--out` on a disk with room, not `/tmp`.
6. **Look at the result**: `<name>.contact.png`, and a few frames at full size
   (extract them with ffmpeg). Check the camera framing, the click rings, that
   captions do not hide the action, and the thumbnail. Render again without
   recording again: `python3 render.py <dir> <name>`.
7. **Hand over** as above. Say what was and was not checked: the audio level is
   measured (−14 LUFS), not listened to, unless the person has played it.

## Where stories go

- `stories/`: merged features. Every story here must pass on master, because
  `qet-verify --videos stories` runs them all.
- `stories/pending/`: features still in an open pull request. Move the story
  to `stories/` when the pull request merges.

## Problems

- The probe is preloaded (`LD_PRELOAD`): it needs the same Qt 6 minor version
  as the binary. "probe did not answer" means a Qt mismatch.
- `'X' is in no menu`: the command was renamed or moved. That is what
  `qet-verify --videos` is for: run it when a pull request renames or moves a
  command.
- YouTube shows chapters only with 3 or more of at least 10 s; `render.py`
  says when a story has fewer.
