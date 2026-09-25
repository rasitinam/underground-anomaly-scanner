"""
Phase 1: environment / tool detection.

Finds QGIS, its bundled Python, ESA SNAP, and checks which required Python
libraries are already installed -- without hardcoding install paths and
without reinstalling anything that is already present.

Works best on Windows (the target platform) but degrades gracefully on
Linux/macOS so it can also be run in a plain dev container.
"""
from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REQUIRED_LIBS = [
    "rasterio",
    "numpy",
    "scipy",
    "requests",
    "shapely",
    "pyproj",
    "pystac_client",
    "planetary_computer",
]
OPTIONAL_LIBS = ["osgeo", "geopandas"]


@dataclass
class QgisInstallation:
    version_hint: str
    install_dir: str
    qgis_python: str | None
    qgis_bin: str | None


@dataclass
class SnapInstallation:
    install_dir: str
    gpt_executable: str


@dataclass
class EnvironmentReport:
    platform: str
    python_executable: str
    python_version: str
    pip_available: bool
    qgis_installations: list[QgisInstallation] = field(default_factory=list)
    snap_installation: SnapInstallation | None = None
    libraries: dict[str, bool] = field(default_factory=dict)
    qgis_python_libraries: dict[str, bool] = field(default_factory=dict)
    internet_available: bool = False
    internet_detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def windows_bat_command(bat: str, args: list[str]) -> str:
    # "call" keeps cmd.exe from stripping the quotes around paths that contain spaces.
    return "call " + " ".join(f'"{a}"' for a in [bat, *args])


def run_interpreter(python: str, args: list[str], timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a Python interpreter or a QGIS python-qgis*.bat launcher with arguments."""
    if python.lower().endswith(".bat"):
        return subprocess.run(windows_bat_command(python, args), shell=True, capture_output=True,
                              text=True, timeout=timeout, env=env)
    return subprocess.run([python, *args], capture_output=True, text=True, timeout=timeout, env=env)


def _run(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        result = run_interpreter(cmd[0], cmd[1:], timeout)
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:  # noqa: BLE001 - report, never crash
        return False, str(exc)


def find_qgis_installations() -> list[QgisInstallation]:
    system = platform.system()
    found: list[QgisInstallation] = []

    if system == "Windows":
        found.extend(_find_qgis_windows())
    elif system == "Darwin":
        found.extend(_find_qgis_macos())
    else:
        found.extend(_find_qgis_linux())

    return found


def _path_from_command(value: str) -> str | None:
    """'"C:\\x y\\bin\\qgis-bin.exe" "%1"' / 'C:\\x\\Uninstall.exe,0' -> the executable path."""
    value = value.strip()
    if not value:
        return None
    if value.startswith('"'):
        return value[1:].split('"', 1)[0]
    lowered = value.lower()
    for ext in (".exe", ".bat", ".ico"):
        idx = lowered.find(ext)
        if idx != -1:
            return value[: idx + len(ext)]
    return value.split(",")[0]


def _qgis_root(path_str: str | None) -> str | None:
    """Turn any file/dir inside a QGIS install into its root (the folder that contains 'bin')."""
    if not path_str:
        return None
    p = Path(os.path.expandvars(path_str.strip().rstrip("\\/")))
    # Folder names like "QGIS 3.40.5" have a "suffix", so test for a real file instead.
    if p.is_file() or (not p.exists() and p.suffix.lower() in (".exe", ".bat", ".ico", ".lnk")):
        p = p.parent
    for candidate in (p, p.parent, p.parent.parent):
        if (candidate / "bin").is_dir():
            return str(candidate)
    return str(p)


def _registry_qgis_dirs() -> set[str]:
    dirs: set[str] = set()
    try:
        import winreg
    except ImportError:
        return dirs

    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in uninstall_roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub_key = winreg.OpenKey(key, winreg.EnumKey(key, i))
                display_name = str(winreg.QueryValueEx(sub_key, "DisplayName")[0])
            except OSError:
                continue
            if "qgis" not in display_name.lower() and "osgeo4w" not in display_name.lower():
                continue
            # NSIS/MSI installers do not always fill InstallLocation; fall back to other values.
            for value_name in ("InstallLocation", "DisplayIcon", "UninstallString", "InstallSource"):
                try:
                    raw = str(winreg.QueryValueEx(sub_key, value_name)[0])
                except OSError:
                    continue
                root = _qgis_root(raw if value_name == "InstallLocation" else _path_from_command(raw))
                if root:
                    dirs.add(root)

    # File association of .qgz/.qgs -> "...\bin\qgis-bin.exe" "%1"
    for ext in (".qgz", ".qgs"):
        try:
            prog_id = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, ext)
            command = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command")
        except OSError:
            continue
        root = _qgis_root(_path_from_command(command))
        if root:
            dirs.add(root)
    return dirs


def _windows_search_roots() -> list[str]:
    roots: list[str] = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        val = os.environ.get(env_var)
        if val:
            roots.append(val)
            if env_var == "LOCALAPPDATA":
                roots.append(str(Path(val) / "Programs"))
    for letter in "CDEFGH":
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            roots += [drive, f"{drive}Program Files", f"{drive}Program Files (x86)"]
    return roots


def _find_qgis_windows() -> list[QgisInstallation]:
    found: list[QgisInstallation] = []
    candidate_dirs: set[str] = set()

    # Optional user override (the folder that contains QGIS's 'bin'), never hardcoded here.
    override = os.environ.get("QGIS_PATH")
    if override:
        candidate_dirs.add(_qgis_root(override) or override)

    for root in _windows_search_roots():
        for pattern in ("QGIS*", "OSGeo4W*"):
            candidate_dirs.update(glob.glob(os.path.join(root, pattern)))

    candidate_dirs.update(_registry_qgis_dirs())

    for name in ("qgis-bin", "qgis-ltr-bin", "qgis", "qgis-ltr"):
        exe = shutil.which(name)
        if exe:
            root = _qgis_root(exe)
            if root:
                candidate_dirs.add(root)

    seen: set[str] = set()
    for d in sorted(candidate_dirs):
        d_path = Path(d)
        key = str(d_path).lower().rstrip("\\")
        if key in seen or not d_path.is_dir():
            continue
        seen.add(key)
        # The python-qgis*.bat launchers set PYTHONHOME/PATH/QT so qgis.core imports.
        qgis_python = _first_match(
            [
                str(d_path / "bin" / "python-qgis-ltr.bat"),
                str(d_path / "bin" / "python-qgis.bat"),
                str(d_path / "bin" / "python3.exe"),
                str(d_path / "apps" / "Python3*" / "python.exe"),
                str(d_path / "apps" / "Python3*" / "python3.exe"),
            ]
        )
        qgis_bin = _first_match(
            [
                str(d_path / "bin" / "qgis-bin.exe"),
                str(d_path / "bin" / "qgis-ltr-bin.exe"),
                str(d_path / "bin" / "qgis.bat"),
                str(d_path / "bin" / "qgis-ltr.bat"),
                str(d_path / "bin" / "qgis.exe"),
            ]
        )
        if qgis_python or qgis_bin:
            found.append(
                QgisInstallation(
                    version_hint=d_path.name,
                    install_dir=str(d_path),
                    qgis_python=qgis_python,
                    qgis_bin=qgis_bin,
                )
            )
    return found


def _find_qgis_linux() -> list[QgisInstallation]:
    found: list[QgisInstallation] = []
    qgis_bin = shutil.which("qgis") or shutil.which("qgis3")
    python_candidates = ["python3"]
    qgis_python = None
    for py in python_candidates:
        py_path = shutil.which(py)
        if not py_path:
            continue
        ok, _ = _run([py_path, "-c", "import qgis.core"], timeout=15)
        if ok:
            qgis_python = py_path
            break
    if qgis_bin or qgis_python:
        found.append(
            QgisInstallation(
                version_hint="system",
                install_dir=str(Path(qgis_bin).parent) if qgis_bin else "unknown",
                qgis_python=qgis_python,
                qgis_bin=qgis_bin,
            )
        )
    flatpak_python = _first_match(
        [str(Path.home() / ".var/app/org.qgis.qgis/**/python3")]
    )
    if flatpak_python:
        found.append(
            QgisInstallation(
                version_hint="flatpak",
                install_dir=str(Path(flatpak_python).parent),
                qgis_python=flatpak_python,
                qgis_bin=None,
            )
        )
    return found


def _find_qgis_macos() -> list[QgisInstallation]:
    found: list[QgisInstallation] = []
    for app_dir in glob.glob("/Applications/QGIS*.app"):
        app_path = Path(app_dir)
        qgis_python = _first_match(
            [str(app_path / "Contents" / "MacOS" / "bin" / "python3")]
        )
        qgis_bin = _first_match([str(app_path / "Contents" / "MacOS" / "QGIS")])
        if qgis_python or qgis_bin:
            found.append(
                QgisInstallation(
                    version_hint=app_path.name,
                    install_dir=str(app_path),
                    qgis_python=qgis_python,
                    qgis_bin=qgis_bin,
                )
            )
    return found


def _first_match(patterns: list[str]) -> str | None:
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        for m in matches:
            if Path(m).exists():
                return m
    return None


def find_snap_installation() -> SnapInstallation | None:
    system = platform.system()
    gpt = shutil.which("gpt")
    if gpt:
        return SnapInstallation(install_dir=str(Path(gpt).parent.parent), gpt_executable=gpt)

    if system == "Windows":
        candidates = _first_match(
            [
                "C:\\Program Files\\*[Ss][Nn][Aa][Pp]*\\bin\\gpt.exe",
                "C:\\Program Files\\esa-snap\\bin\\gpt.exe",
            ]
        )
    elif system == "Darwin":
        candidates = _first_match(["/Applications/esa-snap/bin/gpt"])
    else:
        candidates = _first_match(
            [str(Path.home() / "esa-snap/bin/gpt"), "/opt/esa-snap/bin/gpt", "/usr/local/esa-snap/bin/gpt"]
        )

    if candidates:
        return SnapInstallation(install_dir=str(Path(candidates).parent.parent), gpt_executable=candidates)
    return None


def check_current_python_libraries(libs: list[str] = REQUIRED_LIBS) -> dict[str, bool]:
    import importlib

    result = {}
    for lib in libs:
        try:
            importlib.import_module(lib)
            result[lib] = True
        except Exception:
            result[lib] = False
    return result


def check_qgis_python_libraries(qgis_python: str, libs: list[str] = REQUIRED_LIBS) -> dict[str, bool]:
    result = {}
    for lib in libs:
        ok, _ = _run([qgis_python, "-c", f"import {lib}"], timeout=20)
        result[lib] = ok
    return result


def check_internet(url: str, timeout: int = 8) -> tuple[bool, str]:
    try:
        import requests

        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 500, f"HTTP {resp.status_code} from {url}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def build_environment_report(
    connectivity_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1",
    connectivity_timeout: int = 8,
) -> EnvironmentReport:
    qgis_installs = find_qgis_installations()
    snap_install = find_snap_installation()
    libs = check_current_python_libraries(REQUIRED_LIBS + OPTIONAL_LIBS)

    qgis_libs: dict[str, bool] = {}
    if qgis_installs and qgis_installs[0].qgis_python:
        qgis_libs = check_qgis_python_libraries(
            qgis_installs[0].qgis_python, ["qgis.core", "osgeo.gdal", "numpy"]
        )

    net_ok, net_detail = check_internet(connectivity_url, connectivity_timeout)

    return EnvironmentReport(
        platform=f"{platform.system()} {platform.release()}",
        python_executable=sys.executable,
        python_version=platform.python_version(),
        pip_available=shutil.which("pip") is not None or shutil.which("pip3") is not None,
        qgis_installations=qgis_installs,
        snap_installation=snap_install,
        libraries=libs,
        qgis_python_libraries=qgis_libs,
        internet_available=net_ok,
        internet_detail=net_detail,
    )


def format_report(report: EnvironmentReport) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("ENVIRONMENT REPORT / ORTAM RAPORU")
    lines.append("=" * 60)
    lines.append(f"Platform        : {report.platform}")
    lines.append(f"Python          : {report.python_version} ({report.python_executable})")
    lines.append(f"pip             : {'available' if report.pip_available else 'NOT FOUND'}")
    lines.append("")

    if report.qgis_installations:
        lines.append(f"QGIS            : {len(report.qgis_installations)} installation(s) found")
        for inst in report.qgis_installations:
            lines.append(f"  - {inst.version_hint}: {inst.install_dir}")
            lines.append(f"      QGIS binary : {inst.qgis_bin or 'not found'}")
            lines.append(f"      QGIS python : {inst.qgis_python or 'not found'}")
    else:
        lines.append("QGIS            : NOT FOUND")
        lines.append("  -> Install QGIS (free, official): https://qgis.org/download/")
        lines.append("  -> Already installed in an unusual place? Set the folder that contains QGIS's 'bin':")
        lines.append('     set QGIS_PATH=D:\\path\\to\\QGIS 3.xx      (then run the command again)')
    lines.append("")

    if report.snap_installation:
        lines.append(f"ESA SNAP        : found ({report.snap_installation.gpt_executable})")
    else:
        lines.append("ESA SNAP        : not found (optional; used for proper SAR calibration)")
        lines.append("  -> Optional, free: https://step.esa.int/main/download/snap-download/")
    lines.append("")

    lines.append("Python libraries (current interpreter):")
    for lib, ok in report.libraries.items():
        tag = "[OK]" if ok else ("[optional, missing]" if lib in OPTIONAL_LIBS else "[MISSING]")
        lines.append(f"  {tag} {lib}")
    missing = missing_required(report)
    if missing:
        lines.append(f"  -> Install only the missing ones: python -m pip install {' '.join(PIP_NAMES.get(m, m) for m in missing)}")

    if report.qgis_python_libraries:
        lines.append("")
        lines.append("Python libraries (inside QGIS's own Python):")
        for lib, ok in report.qgis_python_libraries.items():
            lines.append(f"  {'[OK]' if ok else '[MISSING]'} {lib}")

    lines.append("")
    lines.append(f"Internet        : {'available' if report.internet_available else 'NOT AVAILABLE'} ({report.internet_detail})")
    lines.append("=" * 60)
    return "\n".join(lines)


PIP_NAMES = {"pystac_client": "pystac-client", "planetary_computer": "planetary-computer"}


def missing_required(report: EnvironmentReport) -> list[str]:
    return [lib for lib in REQUIRED_LIBS if not report.libraries.get(lib, False)]


def primary_qgis(report: EnvironmentReport) -> QgisInstallation | None:
    for inst in report.qgis_installations:
        if inst.qgis_python:
            return inst
    return report.qgis_installations[0] if report.qgis_installations else None


def save_report_json(report: EnvironmentReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    rep = build_environment_report()
    print(format_report(rep))
