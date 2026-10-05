# Kartograf web app for a public demo, e.g. a Hugging Face Docker Space.
# The model runs at a hosted OpenAI-compatible endpoint; see the KARTOGRAF_*
# variables in server.py. The Frankfurt extract is copied in, not fetched.
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements-server.txt .
RUN pip install --no-cache-dir --user -r requirements-server.txt \
 && python -c "import duckdb; duckdb.connect().execute('INSTALL spatial')"

COPY --chown=user . .

ENV KARTOGRAF_PROVIDER=openai \
    KARTOGRAF_PUBLIC=1 \
    KARTOGRAF_HOST=0.0.0.0 \
    PORT=7860
EXPOSE 7860
CMD ["python", "server.py"]
