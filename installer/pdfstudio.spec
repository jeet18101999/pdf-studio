# PyInstaller spec for PDF Studio (one-folder, windowed build)
# Build with:  installer\build.ps1   (or double-click build_installer.bat)
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hidden = ["PyQt6.QtTest"]      # tiny; lets GUI diagnostics run inside the packaged app
for pkg in ("pyhanko", "pyhanko_certvalidator", "asn1crypto", "oscrypto", "fontTools.ttLib", "ezdxf"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

datas = [(os.path.join(ROOT, "assets", "icon.ico"), "assets"),
         (os.path.join(ROOT, "assets", "icon.png"), "assets")]
for pkg in ("pyhanko", "pyhanko_certvalidator"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6", "PySide2", "scipy", "pandas",
              "sympy", "numba", "torch", "tensorflow", "sklearn", "notebook", "jupyter", "zmq", "win32com",
              "pythoncom", "pywintypes", "setuptools", "pip", "transformers", "huggingface_hub", "tokenizers",
              "safetensors", "accelerate", "datasets", "torchvision", "onnxruntime", "sentencepiece", "nltk",
              "spacy", "boto3", "botocore", "google", "grpc", "tensorboard", "keras", "h5py", "pyarrow",
              "sqlalchemy", "django", "flask", "tornado", "bokeh", "plotly", "seaborn", "statsmodels"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDFStudio",
    icon=os.path.join(ROOT, "assets", "icon.ico"),
    console=False,
    upx=False,
    version=os.path.join(SPECPATH, "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PDFStudio")
