"""Windowed launcher; all application logic lives in BG3ModBridge.py."""
import os
import sys
import traceback
from pathlib import Path

from BG3ModBridge import App, self_test


if "--self-test" in sys.argv:
    self_test()
else:
    try:
        App().mainloop()
    except Exception:
        error = traceback.format_exc()
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "BG3ModBridge"
        local.mkdir(parents=True, exist_ok=True)
        (local / "error.log").write_text(error, encoding="utf-8")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, error, "BG3 Mod Bridge 실행 오류", 0x10)
        finally:
            raise
