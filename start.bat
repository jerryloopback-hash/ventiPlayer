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
.venv312\Scripts\python.exe run.py
pause
