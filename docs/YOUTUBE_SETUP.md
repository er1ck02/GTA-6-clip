# YouTube uploader: one-time setup

Clip Studio posts finished Shorts to your channel by itself, in the background, through the official
YouTube Data API. You do this setup once (about 15 minutes). Nothing is posted until step 6.

## 1. Google Cloud project
1. Go to https://console.cloud.google.com/ and sign in with the Google account that owns the channel.
2. Top bar project picker > **New project**, name it `Clip Studio`, **Create**, then select it.
3. **APIs & Services > Library**, search **YouTube Data API v3**, **Enable**.

## 2. Consent screen
1. **APIs & Services > OAuth consent screen** (also called **Google Auth Platform**) > **Get started**.
2. App name `Clip Studio`, your email as support and developer contact. Audience: **External**.
3. **Data access > Add or remove scopes**: tick `.../auth/youtube.upload` and `.../auth/youtube.readonly`, save.
4. **Audience > Publish app** (move it from Testing to In production). This matters: in Testing mode Google
   expires the sign-in every 7 days and uploads stop. You don't need Google's app verification for your own
   account; you'll just see an "unverified app" warning when signing in.

## 3. OAuth client
1. **APIs & Services > Credentials > Create credentials > OAuth client ID**.
2. Application type **Desktop app**, name `Clip Studio`, **Create**, then **Download JSON**.
3. Save that file as `C:\Users\Erck8\.clipstudio\youtube_client_secret.json`
   (create the `.clipstudio` folder if needed). Keep it private; it's never put in the repo.

## 4. Sign in once
Start Clip Studio, find the **UPLOADS / YOUTUBE SHORTS** panel and press **CONNECT YOUTUBE**. Pick the
channel's account; on "Google hasn't verified this app" choose **Advanced > Go to Clip Studio**, then allow.
The tab says "Signed in as …". (Command line alternative: `clipstudio youtube login`.)

## 5. Ask Google to lift the "private only" lock (the API audit)
Google forces every upload from a new, unaudited API project to **private**, whatever Clip Studio asks for.
Clip Studio still uploads; the panel tells you when a video was locked. To post unlisted or public:

1. Fill in the **YouTube API Services Audit and Quota Extension Form**:
   https://support.google.com/youtube/contact/yt_api_form
2. Say it's a personal tool that uploads your own Shorts to your own channel, one channel, about 6 videos a
   day, using `videos.insert` only; include your Cloud project number (Cloud console home page).
3. Google may ask for screenshots of the sign-in and the upload panel, and a short privacy note. Replies take
   from a few days to a few weeks. The same form raises the daily quota if you ever need more than 6 a day.

## 6. Turn it on
Create `C:\Users\Erck8\.clipstudio\config.yaml` with:

```yaml
upload:
  allow_channels: [ludwig]   # streamers who allow clipping: their clips post automatically
  youtube:
    enabled: true
    privacy: unlisted        # private | unlisted | public
```

Restart Clip Studio. Clips from allowlisted streamers that pass QC now post on their own, at least 45 minutes
apart and at most 6 a day. Everything else lands in the panel as **NEEDS REVIEW** until you press **APPROVE**.

## How it behaves
- **Titles and descriptions** come from the clip's title and always credit the streamer, with links to the
  source stream and their channel. You can edit a title in the panel before it posts. Made for kids is always
  "no"; category is Gaming.
- **Quota**: the free quota is 10,000 units a day and one upload costs about 1,600, so about 6 a day. When it
  runs out the queue pauses until midnight Pacific and carries on.
- **Network trouble**: uploads resume where they stopped (also after a restart) and retry with growing waits,
  then show **FAILED** with the reason and a **RETRY** button.
- **Sign-in revoked or expired**: the panel says so; press **CONNECT YOUTUBE** again.
- Settings live under `upload.youtube` in `configs/default.yaml` (spacing, daily cap, tags, title template).
