# README animations

Regenerates the GIFs in [`docs/assets`](../../docs/assets) from the app's real React UI. A private
Vite dev server serves the frontend, Playwright drives it in headless Microsoft Edge with the Tauri
IPC mocked (`mock-tauri.js`, generic fixture data only), and `make_gif.py` builds the GIFs with Pillow.
The dictation scenes use `demo.html`: a plain editor next to the app's real overlay window, fed with
simulated recognition events.

Requirements: `npm install` done, Microsoft Edge, Python with Pillow (`pip install pillow`).

```powershell
powershell -ExecutionPolicy RemoteSigned -File tools\readme-gifs\Make-ReadmeGifs.ps1            # all scenes
powershell -ExecutionPolicy RemoteSigned -File tools\readme-gifs\Make-ReadmeGifs.ps1 -Scenes refine
```

Scenes: `dictation`, `refine`, `cloud`, `refine-settings` (output `docs/assets/<scene>.gif`).
The script starts Vite on port 5179 (`-Port` to change), records frames to a unique `%TEMP%\handy-readme-gifs-*` folder,
and stops Vite and deletes the frames when done.
