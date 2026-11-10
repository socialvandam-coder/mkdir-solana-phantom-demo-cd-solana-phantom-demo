#!/bin/bash
# usage: batch.sh PAGE OUTNAME [PAGE OUTNAME ...] — renders each page in 4 parallel segments
cd "$(dirname "$0")"
while [ $# -ge 2 ]; do
  P="$1"; NAME="$2"; shift 2
  S="seg_$NAME"; rm -rf "$S"; mkdir "$S"
  F=${FPS:-30}; N=$(( $(PAGE="$P" node render.js count x) * F / 30 )); q=$(( (N + 3) / 4 )); list=""
  for i in 0 1 2 3; do a=$((i*q)); b=$(( (i+1)*q < N ? (i+1)*q : N ))
    PAGE="$P" node render.js video "$S/s$i.mp4" $F $a $b > "$S/log$i.txt" 2>&1 &
    list="${list}file 's$i.mp4'\n"; done; wait
  printf "$list" > "$S/list.txt"
  ffmpeg -v error -y -f concat -safe 0 -i "$S/list.txt" -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
    -map 0:v -map 1:a -c:v copy -c:a aac -b:a 128k -shortest -movflags +faststart "out/$NAME.mp4" && echo "done $NAME $N frames"
done
