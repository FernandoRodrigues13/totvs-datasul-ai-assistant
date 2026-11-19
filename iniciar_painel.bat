@echo off
echo Iniciando Painel Executivo ERP AI Copilot...
call .venv\Scripts\activate.bat
streamlit run streamlit/app.py
pause
