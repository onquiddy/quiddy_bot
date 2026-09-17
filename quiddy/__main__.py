from __future__ import annotations

import asyncio
from pathlib import Path

from .bootstrap import Application


def main() -> None:
    root = Path.cwd()
    app = Application(root)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
