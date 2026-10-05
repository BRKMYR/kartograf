# Deploying the public demo

The demo is the web app (`server.py`) in a Docker Space on Hugging Face, with the
model at a hosted OpenAI-compatible endpoint.

1. Create a Docker Space and set these in its settings:
   - secret `KARTOGRAF_API_KEY`
   - variables `KARTOGRAF_BASE_URL` and `KARTOGRAF_MODELS` (first is the default),
     optionally `KARTOGRAF_RATE`, `KARTOGRAF_DAILY` and `KARTOGRAF_EXTRA_BODY`.
2. Upload: `Dockerfile`, `requirements-server.txt`, the Python modules the server
   imports (`server.py`, `llm.py`, `data_loader.py`, `kepler_view.py`, `scopes.py`,
   `run_evals.py`), `web/`, `evals/questions.yaml`, the Frankfurt extract in
   `data/overture/frankfurt/` and `deploy/space/README.md` as the Space's `README.md`.
3. The Space builds the image and serves on port 7860.

Public mode (`KARTOGRAF_PUBLIC=1`, set in the Dockerfile) limits each visitor to
`KARTOGRAF_RATE` questions per 10 minutes and the whole demo to `KARTOGRAF_DAILY`
questions per day, caps questions at 300 characters, hides internal errors, and
stops any SQL statement after 20 seconds. Set a spending limit at the provider too.
