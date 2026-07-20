@echo off
REM Dobel-klik file ini untuk membuka UI Screener Saham di browser.
cd /d "%~dp0"
echo Menjalankan Screener Saham Indonesia...
echo Browser akan terbuka otomatis. Tutup jendela ini untuk menghentikan.
python -m streamlit run app.py
pause
