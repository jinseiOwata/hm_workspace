@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
echo ==== %date% %time% ==== >> "D:\workspace\line_rakuten_autotrade\run.log"
"C:\Users\hmnks\AppData\Local\Programs\Python\Python312\python.exe" "D:\workspace\line_rakuten_autotrade\morning_push.py" >> "D:\workspace\line_rakuten_autotrade\run.log" 2>&1
