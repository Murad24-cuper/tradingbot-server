#!/bin/sh
set -eu
mkdir -p /opt/tradingbot/data /opt/tradingbot/logs
export WINEPREFIX WINEARCH WINEDEBUG DISPLAY
Xvfb "$DISPLAY" -ac -screen 0 1280x800x24 -nolisten tcp >/opt/tradingbot/logs/xvfb.log 2>&1 &
XVFB_PID=$!
fluxbox >/opt/tradingbot/logs/window-manager.log 2>&1 &
WM_PID=$!
x11vnc -display "$DISPLAY" -auth /dev/null -forever -shared -nopw -rfbport 5900 >/opt/tradingbot/logs/vnc.log 2>&1 &
VNC_PID=$!
cleanup() { kill "$VNC_PID" "$WM_PID" "$XVFB_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
sleep 2
PY='C:\Program Files\Python312\python.exe'
if [ ! -f '/opt/wine/drive_c/Program Files/Python312/python.exe' ]; then
  wine --version >>/opt/tradingbot/logs/wine.log 2>&1 || true
  timeout 300 wineboot --init >>/opt/tradingbot/logs/wine.log 2>&1 || echo 'Wine initialization has a pending GUI or runtime prompt; waiting for Wine readiness.' >>/opt/tradingbot/logs/wine.log
  tries=0
  until timeout 60 wine cmd /c echo WINE_READY >/dev/null 2>&1; do
    tries=$((tries+1)); [ "$tries" -ge 30 ] && { echo 'Wine did not become ready in 30 tries; restarting container.' >>/opt/tradingbot/logs/wine.log; exit 1; }
    sleep 5
  done
  echo 'Wine ready' >>/opt/tradingbot/logs/wine.log
  winetricks -q vcrun2022 >>/opt/tradingbot/logs/wine.log 2>&1 || echo 'VC runtime bootstrap failed; continuing to the Python package import check.' >>/opt/tradingbot/logs/wine.log
  wine /opt/python.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 >>/opt/tradingbot/logs/wine.log 2>&1
  wine "$PY" -m pip install --upgrade pip >>/opt/tradingbot/logs/wine.log 2>&1
fi
# numpy 2.x needs ucrtbase.crealf, missing in Wine's builtin ucrtbase: install Microsoft's ucrtbase once.
UCRT_STAMP=/opt/wine/.ucrtbase_native
if [ ! -f "$UCRT_STAMP" ]; then
  if timeout 600 winetricks -q ucrtbase2019 >>/opt/tradingbot/logs/wine.log 2>&1; then touch "$UCRT_STAMP"; echo 'native ucrtbase installed' >>/opt/tradingbot/logs/wine.log
  else echo 'native ucrtbase install failed' >>/opt/tradingbot/logs/wine.log; fi
fi
# Windows ile eyni paket versiyalari: hemise requirements.txt-den qurulur.
# Fayl deyisibse (git pull), paketler yeniden qurulur.
REQ_STAMP=/opt/wine/.requirements.sha256
REQ_SUM=$(sha256sum /opt/tradingbot/requirements.txt | cut -d' ' -f1)
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP")" != "$REQ_SUM" ]; then
  wine "$PY" -m pip install -r 'Z:\opt\tradingbot\requirements.txt' >>/opt/tradingbot/logs/wine.log 2>&1 && echo "$REQ_SUM" > "$REQ_STAMP"
fi
# No automatic Wine debugger: MT5's protection refuses to run when one attaches after any crash.
wine reg add 'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug' /v Auto /t REG_SZ /d 0 /f >>/opt/tradingbot/logs/wine.log 2>&1 || true
wine reg delete 'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug' /v Debugger /f >>/opt/tradingbot/logs/wine.log 2>&1 || true
find_terminal() { find /opt/wine/drive_c -type f -iname terminal64.exe -print -quit; }
terminal=$(find_terminal)
# 1) The broker's own MT5 folder copied from the Windows computer (same build, broker servers known).
if [ -z "$terminal" ] && [ -f /opt/tradingbot/mt5_install/terminal64.exe ]; then
  mkdir -p '/opt/wine/drive_c/Program Files/XM Global MT5'
  cp -a /opt/tradingbot/mt5_install/. '/opt/wine/drive_c/Program Files/XM Global MT5/'
  echo 'MT5 copied from /opt/tradingbot/mt5_install' >>/opt/tradingbot/logs/wine.log
  terminal=$(find_terminal)
fi
# 2) Official MetaQuotes installer in unattended mode (/auto).
if [ -z "$terminal" ]; then
  winecfg -v win10 >>/opt/tradingbot/logs/wine.log 2>&1 || true
  if curl -fsSL https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe -o /opt/mt5setup.exe; then
    wine /opt/mt5setup.exe /auto >>/opt/tradingbot/logs/wine.log 2>&1 &
    waited=0
    while [ -z "$terminal" ] && [ "$waited" -lt 900 ]; do sleep 10; waited=$((waited+10)); terminal=$(find_terminal); done
    sleep 20; pkill -f terminal64.exe 2>/dev/null || true   # the installer may start the terminal itself
  fi
fi
# 3) Last resort: manual install through VNC (browser opens the broker page).
if [ -z "$terminal" ]; then
  firefox-esr https://www.xm.com/mt5 >/opt/tradingbot/logs/installer-browser.log 2>&1 &
  while [ -z "$terminal" ]; do sleep 10; terminal=$(find_terminal); done
fi
# The bot attaches to exactly this terminal (overrides MT5_PATH from .env, which holds the Windows path).
MT5_PATH=$(winepath -w "$terminal" 2>/dev/null || true)
export MT5_PATH
printf "MT5 terminal: %s\n" "$MT5_PATH" >>/opt/tradingbot/logs/wine.log
# Algo Trading on at every start (MT5 "Platform Start" config; missing keys come from the terminal's own settings).
printf '[Experts]\r\nAllowLiveTrading=1\r\nAllowDllImport=0\r\nEnabled=1\r\nAccount=0\r\nProfile=0\r\n' >/opt/wine/drive_c/tradingbot_mt5.ini
wine "$terminal" /portable '/config:C:\tradingbot_mt5.ini' >/opt/tradingbot/logs/mt5.log 2>&1 &
MT5_PID=$!
tries=0
until timeout 120 wine "$PY" -c 'import numpy, MetaTrader5' >>/opt/tradingbot/logs/wine.log 2>&1; do
  tries=$((tries+1)); [ "$tries" -ge 10 ] && { echo 'Python could not import numpy/MetaTrader5; restarting container.' >>/opt/tradingbot/logs/wine.log; exit 1; }
  sleep 5
done
echo 'Python packages import OK; starting bot' >>/opt/tradingbot/logs/wine.log
# Windows ile eyni mena:
#   bos STOP      = pauza: bot isleyir, yeni emr acmir, aciq pozisiyalari idare edir
#   STOP + HALT   = tam dayanma: bot baslamir
#   supervisor.stop = tam dayanma
halted() {
  [ -e /opt/tradingbot/data/supervisor.stop ] && return 0
  for f in /opt/tradingbot/STOP /opt/tradingbot/STOP.txt; do
    [ -e "$f" ] && grep -qiw HALT "$f" && return 0
  done
  return 1
}
while kill -0 "$MT5_PID" 2>/dev/null; do
  if halted; then sleep 3; continue; fi
  wine "$PY" scripts/run_forever.py >>/opt/tradingbot/logs/bot_stdout.log 2>&1 || true
  sleep 3
done
echo 'MT5 process exited; container will restart and reinitialize it.' >&2
exit 1
