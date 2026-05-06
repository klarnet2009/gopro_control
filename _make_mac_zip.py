"""Build a macOS-ready distributable zip with correct Unix permissions."""
import os
import zipfile
import stat

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP  = os.path.join(PROJECT_DIR, "GoPro-Roll-Call-mac.zip")
FOLDER_NAME = "GoPro Roll Call"   # top-level folder inside the zip

# Files/dirs to include, relative to PROJECT_DIR
INCLUDE_ROOTS = [
    "src",
    "pyproject.toml",
    "config.example.yaml",
    "README.md",
    "install.sh",
    "start.sh",
    "stop.sh",
    "GoPro Roll Call.command",
]

# Patterns to skip anywhere in the tree
SKIP_NAMES = {
    "__pycache__", ".venv", ".git", ".mypy_cache", ".pytest_cache",
    ".gopro_pid", ".gopro_server.log", "cohn_db.json",
    "_make_mac_zip.py",
}
SKIP_EXTS = {".pyc", ".pyo", ".egg-info"}

EXEC_ITEMS = {"install.sh", "start.sh", "stop.sh", "GoPro Roll Call.command"}

def unix_perm(path: str, name: str) -> int:
    """Return Unix permission bits for use as ZipInfo.external_attr >> 16."""
    base_name = os.path.basename(path)
    if base_name in EXEC_ITEMS:
        return 0o755  # rwxr-xr-x
    if os.path.isdir(path):
        return 0o755
    return 0o644  # rw-r--r--

def add_path(zf: zipfile.ZipFile, fs_path: str, arc_path: str) -> None:
    """Recursively add fs_path to the zip as arc_path."""
    name = os.path.basename(fs_path)
    if name in SKIP_NAMES:
        return
    _, ext = os.path.splitext(name)
    if ext in SKIP_EXTS:
        return

    if os.path.isdir(fs_path):
        # Read files inside, but don't create an explicit dir entry for non-empty dirs
        for child in sorted(os.listdir(fs_path)):
            add_path(zf, os.path.join(fs_path, child), f"{arc_path}/{child}")
    else:
        info = zipfile.ZipInfo(arc_path)
        perm = unix_perm(fs_path, name)
        info.external_attr = (perm << 16) | 0o100000 << 16  # regular file + perms
        info.compress_type = zipfile.ZIP_DEFLATED
        with open(fs_path, "rb") as f:
            data = f.read()
        # Normalise line endings to LF for shell scripts
        if name.endswith((".sh", ".command", ".py", ".yaml", ".md", ".txt", ".toml")):
            data = data.replace(b"\r\n", b"\n")
        zf.writestr(info, data)
        print(f"  + {arc_path}")

os.makedirs(PROJECT_DIR, exist_ok=True)

print(f"Building {OUTPUT_ZIP} …\n")
with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
    for item in INCLUDE_ROOTS:
        fs = os.path.join(PROJECT_DIR, item)
        if not os.path.exists(fs):
            print(f"  SKIP (not found): {item}")
            continue
        arc = f"{FOLDER_NAME}/{item}"
        add_path(zf, fs, arc)

size_kb = os.path.getsize(OUTPUT_ZIP) // 1024
print(f"\nDone → {OUTPUT_ZIP}  ({size_kb} KB)")
