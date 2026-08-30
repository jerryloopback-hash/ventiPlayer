import sys
from pathlib import Path

# Check Python version and environment
if sys.version_info < (3, 12):
    print(f"\n[ERROR] VentiPlayer 需要 Python 3.12+ (当前: {sys.version})")
    print(f"请使用项目自带的 .venv312 环境启动：")
    print(f"  方式1: 双击 start.bat")
    print(f"  方式2: .venv312\\Scripts\\python.exe run.py")
    print(f"\n如果在 VS Code 中，请用 Ctrl+Shift+P → 'Python: Select Interpreter'")
    print(f"选择: .venv312\\Scripts\\python.exe")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.main import main

if __name__ == "__main__":
    main()
