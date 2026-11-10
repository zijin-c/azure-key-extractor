"""兼容 python app.py / app:app，所有入口共用 gui.app 的用户隔离后端。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from run_gui import main

    main()
else:
    from gui.app import app
