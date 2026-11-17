# Live Photo Maker

Turn any video into an iOS Live Photo, right in your browser.

**Use it here: https://bonnieferdinand.github.io/live-photo-maker/**

Everything runs locally with [WebCodecs](https://developer.mozilla.org/docs/Web/API/WebCodecs_API), so your video is never uploaded. You need a recent Chrome, Edge or Safari.

## How it works

A Live Photo is two files that share a content identifier (a UUID):

- **JPG**: the cover photo. The ID is stored in an Apple MakerNote (tag `0x0011`).
- **MOV**: a short H.264 clip. The ID is stored in `com.apple.quicktime.content.identifier`, and a timed-metadata track (`com.apple.quicktime.still-image-time`) marks the cover frame.

The page encodes the clip, writes the MOV container itself, and adds the Exif data to the JPG.

## Getting it onto your iPhone

Keep both files together and import them **at the same time**:

- **PC:** upload both files at once at [icloud.com/photos](https://www.icloud.com/photos).
- **Mac:** drag both into Photos, or AirDrop both to your iPhone.

## Desktop version

[`python/live_photo_maker.py`](python/live_photo_maker.py) does the same thing with a small Tkinter GUI or from the command line. It needs Python 3 and ffmpeg.

```
python python/live_photo_maker.py               # GUI
python python/live_photo_maker.py clip.mp4 -l 3 # command line
```
