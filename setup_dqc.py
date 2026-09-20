"""
setup_dqc.py — one-time setup step to make DQC validation available.

The XBRL US Data Quality Committee rules run through a separate Arelle
plugin ("xule", from github.com/xbrlus/xule) that isn't on PyPI and isn't
part of arelle-release. Its own code imports itself as `arelle.plugin.xule`,
so it has to physically live inside Arelle's installed plugin folder — it
can't just be pointed at via an external --plugins path.

Run this once after `pip install -r requirements.txt`:
    python setup_dqc.py

The actual DQC rule set (the compiled rules themselves) is NOT installed by
this script — the plugin downloads and caches whichever taxonomy-year
ruleset a filing needs, automatically, the first time it validates that
filing.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

XULE_REPO = "https://github.com/xbrlus/xule.git"


def get_arelle_plugin_dir() -> Path:
    import arelle
    return Path(arelle.__file__).parent / "plugin"


def is_dqc_installed(plugin_dir: Path) -> bool:
    return (plugin_dir / "xule").is_dir() and (plugin_dir / "validate" / "DQC.py").is_file()


def install_dqc_plugin(plugin_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "xule"
        print(f"Cloning {XULE_REPO} ...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "-q", XULE_REPO, str(tmp_path)],
            check=True,
        )

        xule_src = tmp_path / "plugin" / "xule"
        dqc_src = tmp_path / "plugin" / "validate" / "DQC.py"

        xule_dst = plugin_dir / "xule"
        if xule_dst.exists():
            shutil.rmtree(xule_dst)
        shutil.copytree(xule_src, xule_dst)

        dqc_dst = plugin_dir / "validate" / "DQC.py"
        shutil.copy2(dqc_src, dqc_dst)

    print(f"Installed xule plugin -> {xule_dst}")
    print(f"Installed DQC.py -> {dqc_dst}")


if __name__ == "__main__":
    plugin_dir = get_arelle_plugin_dir()

    if is_dqc_installed(plugin_dir):
        print(f"DQC plugin already installed at {plugin_dir}")
    else:
        install_dqc_plugin(plugin_dir)

    print("\nVerifying plugin activates ...")
    result = subprocess.run(
        [sys.executable, "-m", "arelle.CntlrCmdLine", "--plugins", "validate/DQC", "--about"],
        capture_output=True, text=True,
    )
    print(result.stdout.strip() or result.stderr.strip())
