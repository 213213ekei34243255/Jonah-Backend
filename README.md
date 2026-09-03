# Jonah News/Search Proxy

Holds your NewsAPI and Google Custom Search keys server-side so they
never ship inside the iOS app. Same behavior as the old client-side
calls, just relocated — see the docstring at the top of `app.py` for
exactly what each endpoint does.

## Deploy to Render (matches how your existing `/predict` backend is hosted)

1. Push this folder to its own GitHub repo (or a subfolder of an existing one — just point Render at it).
2. Render dashboard → **New → Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app` (already in the `Procfile`, Render should detect it automatically)
4. **Environment** tab → add:
   - `NEWS_API_KEY` = your newsapi.org key
   - `GOOGLE_API_KEY` = your Google Cloud API key
   - `GOOGLE_CSE_ID` = your Custom Search Engine ID
   - `APP_SHARED_SECRET` = (optional, see below) any random string, e.g. generate one with `python3 -c "import secrets; print(secrets.token_hex(24))"`
5. Deploy. Render gives you a URL like `https://jonah-news-search.onrender.com`.
6. Paste that URL into `BackendConfig.swift` in the iOS project (see the iOS changes below) — that's the one line you need to edit before rebuilding.

## Testing it yourself before wiring up iOS

```bash
# Health check
curl https://YOUR-URL.onrender.com/health

# News (should return NewsAPI's normal JSON shape)
curl "https://YOUR-URL.onrender.com/news/headlines?country=in"

# Web search
curl "https://YOUR-URL.onrender.com/search/web?q=swiftui%20animations"

# Image search
curl "https://YOUR-URL.onrender.com/search/images?q=golden%20retriever"
```

If you set `APP_SHARED_SECRET`, add `-H "X-Jonah-Key: your-secret-here"` to each `curl` command above or you'll get a 401.

## Local testing (before deploying to Render)

```bash
cd jonah-backend
pip install -r requirements.txt

export NEWS_API_KEY=your-key
export GOOGLE_API_KEY=your-key
export GOOGLE_CSE_ID=your-cse-id
# export APP_SHARED_SECRET=something   # optional, leave unset while testing

python3 app.py
# now hit http://localhost:5000/health etc. with curl, same as above
```

## About `APP_SHARED_SECRET` — read this before you assume it "secures" the endpoint

It doesn't, not in the sense the Google/NewsAPI keys needed securing.
Once it's compiled into the iOS app (see `BackendConfig.swift`), it's
just as extractable from the binary as the old API keys were — anyone
willing to open the IPA in a disassembler gets it back. Its only real
job is stopping **casual, anonymous scanning** of your Render URL from
burning through your NewsAPI/Google quota — a bored bot hitting
`/news/headlines` in a loop, not a targeted attacker. If you want
actual per-user rate limiting or abuse protection later, that's a
separate, bigger piece of infrastructure (API gateway, per-device
tokens issued after some auth step, etc.) — out of scope for "make it
match what the iOS app already does."

## Why NewsAPI needed this and Google didn't have to

Google Custom Search keys can be restricted to your app's bundle ID
directly in Google Cloud Console — no backend required for that one.
This proxy exists because NewsAPI has no equivalent restriction
mechanism. I built both endpoints here anyway since you asked for one
backend that mirrors both iOS services — but if you'd rather leave
Google Search calling Google directly (once restricted in Console) and
only proxy NewsAPI, delete the two `/search/*` routes and point
`GoogleSearchService.swift` back at Google directly. Either is a
reasonable choice; this just gives you the option to run both through
one place.
