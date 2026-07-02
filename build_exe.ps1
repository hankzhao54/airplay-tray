param([switch]$Console)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "venv not found. Create it first:`n  py -3.12 -m venv venv`n  venv\Scripts\pip install -r requirements.txt pyinstaller"
    exit 1
}
$mode = if ($Console) { '--console' } else { '--windowed' }
& $py (Join-Path $root 'make_ico.py')
& $py -m PyInstaller --noconfirm --onefile $mode --name AirPlayTray `
  --icon (Join-Path $root 'build\icon.ico') `
  --collect-all pyatv `
  --collect-all zeroconf `
  --collect-all soundcard `
  --collect-all pystray `
  --collect-all pydantic `
  --collect-submodules pyatv `
  --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build') --specpath $root `
  (Join-Path $root 'airplay_tray.py')
Write-Output ("PyInstaller exit: " + $LASTEXITCODE)
