"""``python -m dosojos_sms``: the same CLI without the ``dosojos-sms.exe`` launcher.

Windows Smart App Control blocks that launcher ("An Application Control policy
has blocked this file") because pip builds it unsigned on each machine. Python
itself is signed, so going through it works wherever Python does.
"""

import sys
from pathlib import Path

if getattr(sys.modules.get("dosojos_sms"), "__file__", None) is None:
    # `python -m` puts the current folder first on sys.path, and a folder in it named
    # dosojos_sms (Dos_Ojos/ holds the project) then stands in for this package as an
    # empty namespace. Drop that entry, as -P would, and import the real package.
    here = Path.cwd().resolve()
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != here]
    for name in [n for n in sys.modules if n.split(".")[0] == "dosojos_sms"]:
        del sys.modules[name]

from dosojos_sms.cli import cli  # noqa: E402  (after the path repair above)

if __name__ == "__main__":
    cli()
