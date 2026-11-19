@echo off
echo Iniciando Servidor FastMCP...
call .venv\Scripts\activate.bat
python servers/server_erp.py
pause
