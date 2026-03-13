@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" (call .venv\Scripts\activate.bat)
if exist ".venv\Scripts\streamlit.exe" (.venv\Scripts\streamlit run app.py --server.port 8501) else (streamlit run app.py --server.port 8501)
pause
