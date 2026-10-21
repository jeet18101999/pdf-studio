# Builds PDF Studio as NATIVE code with Nuitka and packs it into the one-click installer.
#
# Nuitka translates the Python source of the app (and of the libraries it uses) into C and compiles that to
# machine code, so the installed program contains no Python source or bytecode to decompile - much harder
# to reverse-engineer or tamper with than a regular Python build.
#
#   1. icon  2. native compile (Nuitka + Zig C compiler)  3. bundled Tesseract OCR  4. self-test
#   5. check that no app source/bytecode was shipped  6. Inno Setup installer
# Output: build\output\PDFStudio-Setup-<version>.exe
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Step "Checking build tools"
# OpenCV: the headless build. The normal opencv-python needs Windows Media Foundation, which is absent
# on Windows N/KN editions and some trimmed installs - there it fails with "DLL load failed importing cv2".
python -m pip uninstall -y opencv-python opencv-contrib-python | Out-Null
python -m pip install --quiet --upgrade nuitka pyhanko ezdxf pikepdf "opencv-python-headless<5" | Out-Null
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is required (https://jrsoftware.org/isdl.php)" }

Step "Generating icon"
python assets\make_icon.py

Step "Compiling to native code with Nuitka (this takes a while)"
$dist = "build\native\main.dist"
if (Test-Path build\native) { Remove-Item build\native -Recurse -Force }
$exclude = @("tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6", "PySide2", "scipy", "pandas", "sympy",
             "numba", "torch", "tensorflow", "sklearn", "notebook", "jupyter", "zmq", "win32com", "pythoncom",
             "pywintypes", "setuptools", "pip", "transformers", "huggingface_hub", "tokenizers", "safetensors",
             "accelerate", "datasets", "torchvision", "onnxruntime", "sentencepiece", "nltk", "spacy", "boto3",
             "botocore", "grpc", "tensorboard", "keras", "h5py", "pyarrow", "sqlalchemy", "django", "flask",
             "tornado", "bokeh", "plotly", "seaborn", "statsmodels", "nuitka", "pyarmor", "PyInstaller")
$nuitka = @("-m", "nuitka", "main.py",
            "--mode=standalone", "--zig", "--assume-yes-for-downloads", "--lto=no",
            "--enable-plugin=pyqt6", "--windows-console-mode=disable",
            "--windows-icon-from-ico=assets\icon.ico",
            "--output-dir=build\native", "--output-filename=PDFStudio.exe",
            "--include-package=pdfeditor",
            "--include-module=PyQt6.QtTest", "--include-module=PyQt6.QtPrintSupport",
            "--include-package-data=pyhanko", "--include-package-data=pyhanko_certvalidator",
            "--include-package-data=docx", "--include-package-data=ezdxf",
            "--include-data-files=assets\icon.ico=assets\icon.ico",
            "--include-data-files=assets\icon.png=assets\icon.png",
            "--company-name=PDF Studio", "--product-name=PDF Studio", "--file-description=PDF Studio",
            "--file-version=1.0.0.0", "--product-version=1.0.0.0",
            "--report=build\native\compilation-report.xml")
foreach ($m in $exclude) { $nuitka += "--nofollow-import-to=$m" }
& python @nuitka
if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed (exit code $LASTEXITCODE)" }
if (-not (Test-Path "$dist\PDFStudio.exe")) { throw "Nuitka did not produce PDFStudio.exe" }

Step "Removing unused libraries"
$prune = @("opencv_videoio_ffmpeg*.dll", "opengl32sw.dll", "Qt6Pdf*.dll", "Qt6VirtualKeyboard*.dll", "_avif*.pyd")
$saved = 0
foreach ($pat in $prune) {
    Get-ChildItem $dist -Recurse -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object { $saved += $_.Length; Remove-Item $_.FullName -Force }
}
Write-Host ("Pruned {0:N0} MB" -f ($saved / 1MB))

Step "Bundling the Microsoft runtime libraries"
# some PCs lack parts of the Visual C++ runtime; shipping them avoids "DLL load failed" on a fresh Windows
foreach ($dll in @("vcomp140.dll", "concrt140.dll", "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")) {
    $src = Join-Path $env:SystemRoot "System32\$dll"
    $dest = Join-Path "$dist" $dll
    if ((Test-Path $src) -and -not (Test-Path $dest)) { Copy-Item $src $dest; Write-Host "  added $dll" }
}

Step "Bundling Tesseract OCR"
$tessSrc = @("$env:ProgramFiles\Tesseract-OCR", "${env:ProgramFiles(x86)}\Tesseract-OCR",
             "$env:LOCALAPPDATA\Programs\Tesseract-OCR") | Where-Object { Test-Path "$_\tesseract.exe" } | Select-Object -First 1
if ($tessSrc) {
    $tessDst = "$dist\tesseract"
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
$p = Start-Process "$dist\PDFStudio.exe" -ArgumentList "--selftest" -PassThru -WindowStyle Hidden
if (-not $p.WaitForExit(120000)) { $p.Kill(); throw "Self-test timed out" }
if ($p.ExitCode -ne 0) { throw "Self-test failed (exit code $($p.ExitCode)); see $env:APPDATA\PDFStudio\selftest.log" }
Write-Host "Self-test passed"

Step "Checking that no app source code or bytecode was shipped"
$leaks = Get-ChildItem $dist -Recurse -Include "*.py", "*.pyc", "*.pyw" -ErrorAction SilentlyContinue |
         Where-Object { $_.FullName -match "pdfeditor" -or $_.Name -eq "main.py" }
if ($leaks) { $leaks | ForEach-Object { Write-Host "  shipped: $($_.FullName)" }; throw "App source or bytecode found in the package" }
Write-Host "No app source or bytecode in the package"

Step "Compiling installer with Inno Setup"
if (Test-Path build\output) { Remove-Item build\output\PDFStudio-Setup-*.exe -Force -ErrorAction SilentlyContinue }
& $iscc /Q "/DDistDir=$Root\$dist" installer\PDFStudio.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit code $LASTEXITCODE)" }
$out = Get-ChildItem build\output\PDFStudio-Setup-*.exe -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $out) { throw "Installer file was not produced" }
Write-Host "`nInstaller ready: $($out.FullName)  ($([math]::Round($out.Length / 1MB)) MB)" -ForegroundColor Green
