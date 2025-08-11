# rawIDE - 1.0 Alpha

Simple vim-like terminal code editor based on python. 
In one file.

It's alpha version. So there may be bugs here.

## Using
```
python3 src/rawIDE.py
```

Only two modes:
**Editor mode** -- esc --> **Command mode**
**Command mode** -- i --> **Editor mode**


## Commands:

```
    :w        - save current file
    :w file   - make & save file
    :wq       - save and quit
    :q        - quit (will warn if unsaved changes)
    :r        - compile and run current file (behavior depends on file extension)
    :open F   - open file F
    :cd DIR   - change working directory
    :mkdir DIR- make directory
    :ls [DIR] - list directory (or current)
    :help     - show available commands
```

## Copyrights?

It's GNU General Public License v3.0
**Feel free for use.**
