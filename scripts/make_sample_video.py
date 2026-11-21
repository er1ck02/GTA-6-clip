"""Generate a labelled SYNTHETIC "gaming stream" test video (no copyrighted footage).

Gameplay: FFmpeg test pattern with a moving 'vehicle' box. Facecam: a framed
panel in the top-left with a moving 'head'. Audio: espeak-ng commentary with
calm stretches and loud reaction moments over a low game-ambience bed.

    python scripts/make_sample_video.py out.mp4 [--short] [--face tests/fixtures/face_nasa_public_domain.jpg]

With --face, the facecam panel shows that (public-domain) photo, gently moving,
so face detection and facecam auto-detection can be tested end to end.

Requires ffmpeg and espeak-ng (or espeak) on PATH.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# (start_seconds, text, gain_db, speed_wpm)
SCRIPT_LONG = [
    (2.0, "Alright chat, we are back in the city. Just cruising around for now, nothing much happening.", 0, 150),
    (9.0, "I am going to drive over to the docks and see if the mission marker is there.", 0, 150),
    (16.0, "The traffic in this part of town is pretty slow. Let me check the map real quick.", 0, 145),
    (24.0, "Okay. I think we take the highway. It should be faster that way.", 0, 145),
    (31.0, "Thanks for the follow by the way, appreciate it. Welcome in.", 0, 150),
    (38.0, "So the plan today is to finish the heist setup missions and maybe do some races later.", 0, 150),
    (47.0, "Wait, wait, what is that? Is that a police car behind us?", 6, 175),
    (52.0, "Oh my god, no way! The car just exploded! Did you see that? That was insane!", 12, 190),
    (58.0, "No no no, they are chasing us now! Go go go! Let's go! We made it! Unbelievable!", 12, 195),
    (65.0, "Oh my gosh, I cannot believe that just happened. Chat, clip that right now!", 9, 180),
    (72.0, "Okay. Okay. Let me calm down. We need to repair the car first.", 0, 145),
    (80.0, "There is a garage somewhere near the beach. I will park and grab some food after this.", 0, 140),
    (88.0, "The weather in game looks really nice right now. The sunset is pretty.", 0, 140),
    (96.0, "Alright, let me look at the settings menu for a second, the sensitivity feels off.", 0, 140),
    (104.0, "Okay that is better. Back to driving. Nothing much going on here.", 0, 140),
]
SCRIPT_SHORT = [
    (1.0, "Alright chat, just cruising around the city for now. Nothing much is happening yet.", 0, 150),
    (8.0, "Wait, what is that? Oh my god, no way! The car just exploded! Did you see that? That was insane!", 12, 190),
    (15.0, "No no no, they are chasing us! Go go go! Let's go! We made it! Unbelievable!", 12, 195),
    (22.0, "Okay, let me calm down. We need to repair the car first and then grab some food.", 0, 145),
]


def run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--short", action="store_true", help="30 s version for fast tests")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--face", type=Path, help="image shown inside the facecam panel")
    a = ap.parse_args()
    tts = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts:
        print("espeak-ng is required (apt install espeak-ng / brew install espeak-ng)", file=sys.stderr)
        return 1
    script = SCRIPT_SHORT if a.short else SCRIPT_LONG
    total = 30.0 if a.short else 112.0
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        inputs, filters = [], []
        for i, (t, text, gain, wpm) in enumerate(script):
            wav = tdp / f"l{i}.wav"
            run([tts, "-v", "en-us", "-s", str(wpm), "-p", "55" if gain else "45", "-w", str(wav), text])
            inputs += ["-i", str(wav)]
            ms = int(t * 1000)
            filters.append(f"[{i + 1}:a]aresample=48000,volume={gain}dB,adelay={ms}|{ms},apad=whole_dur={total}[s{i}]")
        n = len(script)
        mix = "".join(f"[s{i}]" for i in range(n))
        # ambience bed: brown noise + low hum, quiet
        filters.append(f"[0:a]volume=-34dB[bed]")
        filters.append(f"[bed]{mix}amix=inputs={n + 1}:normalize=0:duration=first,alimiter=limit=0.95[aout]")
        audio = tdp / "audio.wav"
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.6:d={total}:r=48000",
             *inputs, "-filter_complex", ";".join(filters), "-map", "[aout]", "-ac", "2", str(audio)])

        W, H = 1920, 1080
        vf = (
            f"[0:v]hue=s=0.6,eq=brightness=-0.05[bg];"
            # moving 'vehicle'
            f"[1:v]format=rgba[car];[bg][car]overlay=x='800+500*sin(t*0.6)':y='620+120*sin(t*1.3)'[g1];"
            # facecam panel with a moving 'head'
            f"[2:v]drawbox=x=0:y=0:w=iw:h=ih:color=0xFF5A6E:t=8,"
            f"drawbox=x='200+40*sin(t*2)':y='90+20*sin(t*3)':w=110:h=140:color=0xE8C39E:t=fill[cam];"
            f"[g1][cam]overlay=x=40:y=40[g2];"
            f"[g2]drawtext=text='SYNTHETIC SAMPLE - NOT REAL GAMEPLAY':x=w-tw-40:y=h-th-30:fontsize=28:fontcolor=white@0.8:"
            f"box=1:boxcolor=black@0.5:boxborderw=8,"
            f"drawtext=text='%{{pts\\:hms}}':x=w-tw-40:y=40:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.5[v]"
        )
        extra = []
        if a.face:
            # photo (cover-scaled into the 480x300 panel, drifting slightly) instead of the drawn 'head'
            vf = vf.replace(
                f"[2:v]drawbox=x=0:y=0:w=iw:h=ih:color=0xFF5A6E:t=8,"
                f"drawbox=x='200+40*sin(t*2)':y='90+20*sin(t*3)':w=110:h=140:color=0xE8C39E:t=fill[cam];",
                f"[4:v]scale=520:-2,crop=480:300:x='20+18*sin(t*0.9)':y='(ih-300)/3+10*sin(t*1.4)',"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=0xFF5A6E:t=8[cam];")
            extra = ["-loop", "1", "-framerate", str(a.fps), "-t", str(total), "-i", str(a.face)]
        run(["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={a.fps}:duration={total}",
             "-f", "lavfi", "-i", f"color=c=0x2AB7CA:size=220x120:rate={a.fps}:duration={total}",
             "-f", "lavfi", "-i", f"color=c=0x30343F:size=480x300:rate={a.fps}:duration={total}",
             "-i", str(audio), *extra, "-filter_complex", vf, "-map", "[v]", "-map", "3:a",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-shortest", "-metadata", "title=Clip Studio synthetic sample",
             str(a.out)])
    print(f"wrote {a.out} ({total:.0f}s, synthetic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
