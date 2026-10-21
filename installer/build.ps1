# Builds the PDF Studio Windows installer:
#   1. icon  2. PyInstaller app folder  3. bundled Tesseract OCR (trimmed)  4. Inno Setup installer
# Output: build\output\PDFStudio-Setup-<version>.exe
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Step "Checking build tools"
# OpenCV: the headless build. The normal opencv-python needs Windows Media Foundation, which is absent
# on Windows N/KN editions and some trimmed installs - there it fails with "DLL load failed importing cv2".
python -m pip uninstall -y opencv-python opencv-contrib-python | Out-Null
python -m pip install --quiet --upgrade pyinstaller pyhanko ezdxf pikepdf "opencv-python-headless<5" | Out-Null
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Host "Inno Setup not found - installing with winget..."
    winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements --silent
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 is required (https://jrsoftware.org/isdl.php)" }
}

Step "Generating icon"
python assets\make_icon.py

Step "Building application with PyInstaller"
if (Test-Path build\dist) { Remove-Item build\dist -Recurse -Force }
python -m PyInstaller --noconfirm --clean --distpath build\dist --workpath build\work installer\pdfstudio.spec
if (-not (Test-Path build\dist\PDFStudio\PDFStudio.exe)) { throw "PyInstaller build failed" }

Step "Removing unused libraries"
$prune = @("_internal\cv2\opencv_videoio_ffmpeg*.dll",        # video I/O - never used
           "_internal\PyQt6\Qt6\bin\opengl32sw.dll",           # software OpenGL - widgets app does not need it
           "_internal\PyQt6\Qt6\bin\Qt6Pdf*.dll",              # Qt's own PDF module - PyMuPDF is used instead
           "_internal\PyQt6\Qt6\bin\Qt6Quick*.dll", "_internal\PyQt6\Qt6\bin\Qt6Qml*.dll",
           "_internal\PyQt6\Qt6\bin\Qt6VirtualKeyboard*.dll",
           "_internal\PyQt6\Qt6\translations\qtwebengine*", "_internal\PIL\_avif*.pyd")
$saved = 0
foreach ($pat in $prune) {
    Get-ChildItem "build\dist\PDFStudio\$pat" -ErrorAction SilentlyContinue | ForEach-Object { $saved += $_.Length; Remove-Item $_.FullName -Force }
}
Write-Host ("Pruned {0:N0} MB" -f ($saved / 1MB))

Step "Bundling the Microsoft runtime libraries"
# some PCs lack parts of the Visual C++ runtime; shipping them avoids "DLL load failed" on a fresh Windows
foreach ($dll in @("vcomp140.dll", "concrt140.dll", "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")) {
    $src = Join-Path $env:SystemRoot "System32\$dll"
    $dest = Join-Path "build\dist\PDFStudio" $dll
    if ((Test-Path $src) -and -not (Test-Path $dest)) { Copy-Item $src $dest; Write-Host "  added $dll" }
}

Step "Bundling Tesseract OCR"
$tessSrc = @("$env:ProgramFiles\Tesseract-OCR", "${env:ProgramFiles(x86)}\Tesseract-OCR",
             "$env:LOCALAPPDATA\Programs\Tesseract-OCR") | Where-Object { Test-Path "$_\tesseract.exe" } | Select-Object -First 1
if ($tessSrc) {
    $tessDst = "build\dist\PDFStudio\tesseract"
    New-Item -ItemType Directory -Force "$tessDst\tessdata" | Out-Null
    Get-ChildItem $tessSrc -File | Where-Object { $_.Extension -in ".exe", ".dll" -or $_.Name -like "*LICENSE*" -or $_.Name -like "*COPYING*" } |
        Copy-Item -Destination $tessDst
    foreach ($d in "configs", "tessconfigs") {
        if (Test-Path "$tessSrc\tessdata\$d") { Copy-Item "$tessSrc\tessdata\$d" "$tessDst\tessdata\$d" -Recurse -Force }
    }
    foreach ($lang in "eng", "osd", "hin", "guj") {
        if (Test-Path "$tessSrc\tessdata\$lang.traineddata") { Copy-Item "$tessSrc\tessdata\$lang.traineddata" "$tessDst\tessdata\" }
    }
    Write-Host "Tesseract bundled from $tessSrc"
} else {
    Write-Warning "Tesseract not found - the installer will work but OCR needs Tesseract installed separately."
}

Step "Smoke-testing the built app"
$p = Start-Process build\dist\PDFStudio\PDFStudio.exe -ArgumentList "--selftest" -PassThru -WindowStyle Hidden
if (-not $p.WaitForExit(90000)) { $p.Kill(); throw "Self-test timed out" }
if ($p.ExitCode -ne 0) { throw "Self-test failed (exit code $($p.ExitCode)); see $env:APPDATA\PDFStudio\selftest.log" }
Write-Host "Self-test passed"

Step "Compiling installer with Inno Setup"
if (Test-Path build\output) { Remove-Item build\output\PDFStudio-Setup-*.exe -Force -ErrorAction SilentlyContinue }
& $iscc /Q installer\PDFStudio.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit code $LASTEXITCODE)" }
$out = Get-ChildItem build\output\PDFStudio-Setup-*.exe -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $out) { throw "Installer file was not produced" }
Write-Host "`nInstaller ready: $($out.FullName)  ($([math]::Round($out.Length / 1MB)) MB)" -ForegroundColor Green
