"""Original formats for the YouTube channel: content we write and voice ourselves around credited clips.

Why this exists: YouTube treats channels of raw streamer clips as "reused content" and keeps them out of the
Partner Program. Its policy allows "edited footage from other creators where you add a storyline and
commentary", so these formats add our own ranking, script and narration:

* :mod:`clipstudio.formats.top5`  weekly "top 5 GTA 6 streamer moments" (16:9 long-form, 4-8 min) plus one
  vertical Short cut-down per moment.
* :mod:`clipstudio.formats.news`  GTA 6 news / easter-egg Shorts. Every factual line must cite a source.

Both are driven by an episode spec (JSON) that holds the script; :mod:`clipstudio.formats.voice` speaks it with
a free neural voice. Nothing here uploads anything: the build writes the video, a thumbnail and ``upload.json``
(title, description with credits/sources/chapters, tags, visibility "unlisted").
"""
