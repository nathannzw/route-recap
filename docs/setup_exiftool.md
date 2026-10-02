# ExifTool Setup (Windows)

`route-recap` uses [ExifTool](https://exiftool.org/) as its primary metadata
extractor because it handles every iPhone media format uniformly
(HEIC/HEIF, JPEG, MOV, MP4) — including GPS and timestamps.

> Pillow + `pillow-heif` is used as an automatic fallback for images only.
> For videos (MOV/MP4) ExifTool is effectively required.

## Install

Pick one:

- **winget (recommended):**
  ```powershell
  winget install OliverBetz.ExifTool
  ```
- **Chocolatey:**
  ```powershell
  choco install exiftool
  ```
- **Scoop:**
  ```powershell
  scoop install exiftool
  ```
- **Manual:** download the Windows executable from
  <https://exiftool.org/> and put it on your `PATH` (or point
  `EXIFTOOL_PATH` at it in `.env`).

## Verify

```powershell
exiftool -ver
```

Should print a version like `13.10`. If the command is not found, open a
new terminal (PATH refresh) or set `EXIFTOOL_PATH` in your `.env`.
