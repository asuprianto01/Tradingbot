@echo off
REM Dijalankan Windows Task Scheduler tiap hari kerja 09:20 WIB.
REM Scan intraday + kirim Telegram HANYA jika ada sinyal (gocap lalu liquid).
cd /d C:\xampp\htdocs\TradingBot
C:\Python313\python.exe ara_scanner.py --alert --profile gocap  >> alert.log 2>&1
C:\Python313\python.exe ara_scanner.py --alert --profile liquid >> alert.log 2>&1
