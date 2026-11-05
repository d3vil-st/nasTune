#!/bin/sh
# Build a minimal ffmpeg + ffprobe for nasTune into PREFIX.
#
# The distro ffmpeg package pulls ~355 MB of dependencies into the image (LLVM
# and Mesa via libplacebo/Vulkan, flite, x265, SVT-AV1, …). nasTune only uses
# ffmpeg for:
#   - ALAC → FLAC for browser playback (transcode_cache.py, audio only)
#   - embedded cover extraction, downscaled to JPEG (artwork_cache.py)
#   - ffprobe tag reads (scanner.py)
# Sync transcoding is done by gpod-cp, which links its own static ffmpeg.
#
# Build deps: gcc make pkg-config git ca-certificates zlib dev, nasm (x86 only)
set -eu

PREFIX=${1:?usage: $0 <prefix>}
FFMPEG_REPO=${FFMPEG_REPO:-https://git.ffmpeg.org/ffmpeg.git}
# release/9.1 — same ref as gpod-utils' ci/build-ffmpeg.sh
FFMPEG_REF=${FFMPEG_REF:-f8e017db6bdac7e1d40ce24a286cf91149513877}
JOBS=${JOBS:-$(nproc)}

SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT

git -C "$SRC" init -q
git -C "$SRC" fetch -q --depth 1 "$FFMPEG_REPO" "$FFMPEG_REF"
git -C "$SRC" checkout -q FETCH_HEAD

ASM=--enable-x86asm
case "$(uname -m)" in
  x86_64|i?86) ;;
  *) ASM= ;;
esac

# Demuxers cover every format a source/iPod/WALKMAN library may hold, so cover
# art (attached picture streams) and tags can be read from any of them.
cd "$SRC"
./configure \
  --prefix="$PREFIX" \
  --enable-static --disable-shared \
  --disable-doc --disable-debug --disable-network --disable-autodetect \
  --disable-everything \
  --enable-ffmpeg --enable-ffprobe --disable-ffplay \
  $ASM \
  --enable-zlib \
  --enable-swresample --enable-swscale \
  --enable-protocol=file,pipe \
  --enable-demuxer=aac,aiff,ape,asf,caf,dsf,flac,matroska,mov,mp3,ogg,tta,w64,wav,wv \
  --enable-decoder=alac,mjpeg,png,bmp,gif,webp \
  --enable-parser=aac,flac,mpegaudio,opus,vorbis,mjpeg,png,bmp,gif,webp \
  --enable-encoder=flac,mjpeg \
  --enable-muxer=flac,image2 \
  --enable-filter=scale,format,null,aresample,aformat,anull
make -j"$JOBS"
make install
strip "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe"
