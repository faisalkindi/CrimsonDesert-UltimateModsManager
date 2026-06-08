CDUMM v3.3.18

Recovery no longer loops forever after a game update. On some setups, clicking Start Recovery took a fresh snapshot, but the next launch said the files did not match and asked to recover again, over and over. The snapshot was saving the file list correctly but failing to record the new game version, because the database was briefly locked and that error was hidden. The game version is now saved as part of the snapshot itself, so it sticks and the prompt stops coming back. Thanks to xenoi60 (#163).

The preset chooser keeps its Install button on screen with long preset lists. Mods that ship a lot of presets grew the Choose Mod Preset(s) window taller than the screen, so the Install and Cancel buttons ended up below the bottom edge where you could not click them. The preset list now scrolls inside the window instead of pushing the buttons off, so they stay visible at any window size. Thanks to lupo1190 (#200).

Character creator mods now ask which race and gender to install. Mods that ship separate body folders per race (Human, Goblin, Orc, male and female) alongside a few JSON option files were jumping straight to the JSON options and never showing the race picker, so there was no way to choose your character. The race and gender picker now comes first for these mods. Thanks to lurkser and woowoots (#190).
