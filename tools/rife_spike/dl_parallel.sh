#!/usr/bin/env bash
# 分段并行下载 models.v15.13.7z（813MB），绕过单连接限速。
# 用法: dl_parallel.sh <url> <out> <total_size> <nchunks>
set -e
URL="$1"; OUT="$2"; SIZE="$3"; N="${4:-16}"
CHUNK=$(( SIZE / N + 1 ))
echo "total=$SIZE chunk=$CHUNK x $N"
pids=()
for i in $(seq 0 $((N-1))); do
  start=$(( i * CHUNK ))
  end=$(( start + CHUNK - 1 ))
  if [ $end -ge $SIZE ]; then end=$(( SIZE - 1 )); fi
  curl -L -sS -r "$start-$end" -o "$OUT.part$i" "$URL" &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p" || exit 1; done
cat $(for i in $(seq 0 $((N-1))); do echo "$OUT.part$i"; done) > "$OUT"
rm -f "$OUT".part*
ls -la "$OUT"
echo PARALLEL_DL_DONE
