@echo off
cd /d "%~dp0"
set MIOPEN_USER_DB_PATH=%~dp0.miopen_cache
set MIOPEN_CUSTOM_CACHE_DIR=%~dp0.miopen_cache
rem MIOpen tuning: FAST find mode skips the exhaustive conv-solver search
rem (the source of the GemmFwdRest workspace warning flood + slow first run);
rem LOG_LEVEL=2 silences those harmless warnings.
set MIOPEN_FIND_MODE=FAST
set MIOPEN_LOG_LEVEL=2
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set PYTORCH_ALLOC_CONF=expandable_segments:True
if not exist "%~dp0.miopen_cache" mkdir "%~dp0.miopen_cache"
rem stdout/stderr 重定向到日志：原生闪退（MIOpen/HIP abort 等）的报错也能留痕，
rem 每次启动覆盖上次日志，只保留最近一次运行的完整输出。
if not exist "%~dp0log" mkdir "%~dp0log"
.venv312\Scripts\python.exe run.py > "%~dp0log\ventiplayer.log" 2>&1
echo 程序已退出。若发生闪退，请把 log\ventiplayer.log 发给开发者分析。
pause
