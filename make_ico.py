"""Generate the multi-resolution icon.ico for the exe (run by build_exe.ps1)."""
import os

from airplay_tray import make_icon

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build", "icon.ico")
os.makedirs(os.path.dirname(out), exist_ok=True)
make_icon(active=False, size=256).save(
    out, format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
