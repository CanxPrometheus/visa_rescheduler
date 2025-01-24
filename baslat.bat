@echo off
title Visa Rescheduler
echo Uygulama başlatılıyor...
:: Python yolu kontrol ediliyor
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo Python yüklü değil veya PATH'e ekli değil. Lütfen Python yükleyin ve PATH'e ekleyin.
    pause
    exit
)

:: Uygulamayı başlat
python run.py

:: Uygulama sona erdiğinde bir mesaj göster ve pencereyi kapatma
echo.
echo Uygulama sona erdi. Kapatmak için bir tuşa basın...
pause