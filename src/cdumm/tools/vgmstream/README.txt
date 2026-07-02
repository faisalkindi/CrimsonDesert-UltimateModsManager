vgmstream — Wwise audio decoder for the Game Data tab
=====================================================

The game's sounds (.wem / .bnk) are Wwise Vorbis, which Windows cannot play
directly. The Game Data tab uses vgmstream to decode them to standard WAV for
the in-app "Play" button and "Export as WAV".

To enable audio playback, drop the vgmstream CLI here:

    src/cdumm/tools/vgmstream/vgmstream-cli.exe

Download it from the official project (pick the Windows CLI build):

    https://github.com/vgmstream/vgmstream
    Releases: https://github.com/vgmstream/vgmstream/releases
    (the file is vgmstream-cli.exe, inside vgmstream-win64.zip / vgmstream-win.zip)

The app auto-detects it here, or anywhere on the system PATH. Until it's
present, audio previews still show full Wwise metadata and the raw .wem/.bnk
"Extract raw file" always works — only Play / Export as WAV need this binary.

vgmstream is a separate open-source project with its own license; it is NOT
part of CDUMM. This folder only tells the app where to find it.
