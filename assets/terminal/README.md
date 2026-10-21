# Mikachan terminal appearance

The installed asset directory is `/opt/Mikachan/assets/terminal/`. A source
checkout uses its own `assets/terminal/` directory. Managed updates preserve
files already placed here.

- `intro/intro.txt` contains the default native animation script. It is treated
  as presentation text; lines beginning with `$` are never executed.
- Add your own `intro/intro.mp4` or `intro/intro.gif` for optional media playback.
  The MP4 takes precedence when both are valid. Limit files to 25 MiB, 20 seconds,
  1920×1080. Playback is silent monochrome characters through `ffmpeg` and
  requires an SSH terminal with at least 80×24 cells and color support. If
  unsupported, the native text animation plays instead.
- Place printable multiline ASCII art in `artwork/header.txt`. Keep it within
  16 KiB, 40 lines and 200 columns. Existing spaces are preserved. Missing or
  invalid artwork falls back to a built-in Mikachan title.
- `appearance.json` is created automatically when you save preferences in the
  **Appearance** screen. It is not a secret file; the panel owns it.

Launch the installed panel with `mikachan`. Open **Appearance** to preview the
intro or header, reload changed files, switch alignment, choose Auto / Native /
Media playback, and turn startup playback off. Simply replacing an asset file
is enough; the next interactive launch reads it automatically. The bot service
is not restarted. Press Space or Enter to skip the intro when skipping is on.

To preview before installation from a checkout, use an interactive terminal:

```bash
python3 -m deploy.manager_ui
```

The panel reads the checkout's local assets in that case. Configuration and
management actions still target the configured host installation.
