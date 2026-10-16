#!/bin/bash
# Залить сборку по USB и посмотреть, встала ли она (или откат). $1 = путь к .bin
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
n=$(date +%s)
cp "$1" ../Прошивки/139.71.$n.bin || exit 1
echo "файл 139.71.$n.bin ($(stat -c %s "$1") Б) из $1"
python fw_usb_update.py ../Прошивки/139.71.$n.bin COM5 2>&1 | tail -1
start=$(date +%s)
last=""
for i in $(seq 1 40); do
  sleep 5
  s=$(python next_status.py 2>&1 | tail -1)
  b=$(echo "$s" | grep -o 'build=[0-9]*')
  c=$(echo "$s" | grep -o 'connected=[0-9]')
  now="$b $c"
  if [ "$now" != "$last" ]; then echo "$(( $(date +%s) - start )) с: $now"; last="$now"; fi
  if [ "$b" = "build=$n" ] && [ "$c" = "connected=1" ] && [ $(( $(date +%s) - start )) -gt 60 ]; then echo "ВСТАЛА: build=$n на связи"; exit 0; fi
done
echo "ИТОГ: $last"
