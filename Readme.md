# Find the Fuckup · India

**Drop a contract, get the fuckups.** AI-powered contract analyser tuned for Indian commercial paper — DPDPA, FEMA, GST, stamp duty, Indian Contract Act, plus the usual drafting sins.

Built by [Sigil](https://sigil91.com).

## Indian-law issues the analyser is tuned to catch

- **DPDPA 2023 gaps** — missing consent flows, no 72-hour breach notification, no data principal rights, cross-border transfer issues
- **Indian Contract Act 1872** — failure for want of consideration, Section 27 restraint-of-trade, Section 74 penalty vs liquidated damages
- **FEMA implications** — USD payment terms, intercompany agreements that look like undisclosed ECB
- **GST** — wrong place-of-supply language, missing GST inclusive/exclusive clarity, no tax invoice obligation
- **Stamp Duty** — unstamped contracts, multi-state execution issues, missing e-stamp acknowledgment
- **IT Act 2000** — electronic signature clauses without Section 5 / 3A reference
- **Arbitration & Conciliation Act** — seat vs venue (BALCO), unclear governing law
- **Companies Act 2013** — Section 188 RPTs, Section 197 director indemnity
- **Limitation Act 1963** — Section 28 issues with contractually shortened limitation
- **Cross-border** — foreign-law MSAs signed by Indian entities without local-law carve-outs

## Tech stack

Flask + React (CDN) + **Gemini 2.5 Flash** (swapped from upstream's Claude Sonnet — Gemini's free tier is more forgiving for an MVP) + PostgreSQL + gunicorn.

Swap models any time via the `GEMINI_MODEL` env var: `gemini-2.5-flash` (default, free tier), `gemini-2.5-pro` (better quality, paid), `gemini-2.5-flash-lite` (fastest).

## Run locally

```bash
# 1. Install
pip install -r requirements.txt

# 2. Set env vars (see .env.example)
export GEMINI_API_KEY=AIza...
export RECAPTCHA_SECRET_KEY=...
export DATABASE_URL=postgresql://user:password@localhost:5432/findthefuckup

# 3. Run
python app.py
# OR for production
gunicorn app:app
```

The app runs on `http://localhost:5000`.

## Required secrets

| Variable | Why | Where to get it |
|---|---|---|
| `GEMINI_API_KEY` | Powers the analysis | aistudio.google.com/app/apikey (sign in with Google, free) |
| `RECAPTCHA_SECRET_KEY` | Bot protection | google.com/recaptcha/admin (v3) |
| `GEMINI_MODEL` | Optional — model selector | Default: `gemini-2.5-flash` |
| `DATABASE_URL` | Optional — email tracking + shareable error gallery | Postgres anywhere (Render auto-provisions, or local) |

Also update the reCAPTCHA **site key** in `static/index.html` (currently uses the upstream key — will not work for new domains).

## Deploy

### Render (one-click)
1. New Web Service → connect this repo
2. Add the 3 env vars
3. Add a free Postgres instance (Render auto-fills `DATABASE_URL`)
4. Done — point your domain at the Render service

### Vercel + Railway / Fly.io
This is a Python/Flask app, so Vercel is awkward. Cleaner: deploy backend to Railway/Fly, point static at Vercel, OR just put everything on Render.

## Customisation knobs

- **Tweak what Claude looks for** → edit the `prompt` variable in `app.py` (`check_document()` function)
- **Rate limits** → `@limiter.limit("5 per hour")` decorator on `/api/check-document`
- **Blocked free email domains** → `validate_email()` in `app.py`
- **File size limit** → `MAX_CONTENT_LENGTH` in `app.py` (default 10 MB)
- **Styling** → `static/index.html` `<style>` block

## License

MIT.

## Disclaimer

This is a starting-point tool to find common errors. Not legal advice. Does not create an attorney-client relationship. For real review — drafted, redlined, signed off by a Bar Council-enrolled advocate — go to [sigil91.com](https://sigil91.com).
