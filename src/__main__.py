"""``python -m dosojos_drone``: the same CLI without the ``dosojos-drone.exe`` launcher.

Windows Smart App Control blocks that launcher ("An Application Control policy
has blocked this file") because pip builds it unsigned on each machine. Python
itself is signed, so going through it works wherever Python does.
"""

import sys
from pathlib import Path

if getattr(sys.modules.get("dosojos_drone"), "__file__", None) is None:
    # `python -m` puts the current folder first on sys.path, and a folder in it named
    # dosojos_drone (Dos_Ojos/ holds the project, each demo a workspace) then stands
    # in for this package as an empty namespace, able to hide submodules behind data
    # folders. Drop that entry, as -P would, and import the real package.
    here = Path.cwd().resolve()
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != here]
    for name in [n for n in sys.modules if n.split(".")[0] == "dosojos_drone"]:
        del sys.modules[name]

from dosojos_drone.cli import cli  # noqa: E402  (after the path repair above)

if __name__ == "__main__":
    cli()
