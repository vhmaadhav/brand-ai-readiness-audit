"""Render methodology-video.html to MP4 (1920x1080) plus an .srt subtitle file.

Frame-exact: each frame calls the page's seek(t), so the output does not depend
on machine speed. Needs: pip install playwright, ffmpeg on PATH, Chrome installed.

    python round4/render_video.py            # burned-in subtitles
    python round4/render_video.py --nosubs   # clean video (use the .srt instead)
"""
import argparse
import pathlib
import subprocess

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent


def srt_time(s: float) -> str:
    ms = round(s * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--nosubs", action="store_true")
    ap.add_argument("--out", default=str(HERE / "part1-methodology.mp4"))
    args = ap.parse_args()

    url = (HERE / "methodology-video.html").as_uri() + "?render=1" + ("&nosubs=1" if args.nosubs else "")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(url, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        total = page.evaluate("TOTAL")
        cues = page.evaluate("CUES")

        srt = "\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1))
        (HERE / "part1-methodology.srt").write_text(srt, encoding="utf-8")

        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(args.fps),
             "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
             "-preset", "medium", "-movflags", "+faststart", args.out],
            stdin=subprocess.PIPE)
        frames = int(total * args.fps)
        for f in range(frames):
            page.evaluate(f"seek({f / args.fps})")
            ff.stdin.write(page.screenshot(type="jpeg", quality=92))
            if f % (args.fps * 10) == 0:
                print(f"{f / args.fps:5.0f}s / {total}s", flush=True)
        ff.stdin.close()
        ff.wait()
        browser.close()
    print("wrote", args.out)


if __name__ == "__main__":
    main()
