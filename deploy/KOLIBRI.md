# Running Kartograf against Kolibri-1

[Kolibri-1](https://huggingface.co/Aleph-Alpha/Kolibri-1) is Aleph Alpha's open-weight
German-English model (Apache 2.0, 78B total, 3.46B active per token, FP8 weights). It
does not fit on a laptop: the model card names 2x A100 80 GB, 2x H100, or one H200,
B200 or B300 as the minimum. No hosted inference provider serves it yet, so the
setup is a rented GPU running vLLM, reached through an SSH tunnel. Kartograf
already talks to any OpenAI-compatible endpoint, so nothing in the code changes.

## 1. Serve the model on the GPU machine

On a fresh Linux GPU instance with CUDA (one H200 is the simplest choice):

```bash
pip install 'aleph-alpha-inference>=1'
export VLLM_API_KEY=$(openssl rand -hex 16); echo "$VLLM_API_KEY"   # copy this
vllm serve Aleph-Alpha/Kolibri-1 --kv-cache-dtype fp8 \
  --reasoning-parser kolibri1 \
  --tool-call-parser kolibri1 \
  --enable-auto-tool-choice \
  --max-model-len 32768 \
  --host 127.0.0.1 --port 8000
```

`--tool-call-parser kolibri1 --enable-auto-tool-choice` turns on tool calling, which
the `run_sql` loop needs. The reasoning parser moves the model's thinking out of the
answer text. Kartograf prompts are short, so a 32k context saves memory. The server
listens on localhost only and is never exposed to the internet.

The first start downloads about 80 GB of weights. Stop the instance when you are
done: it bills by the hour.

## 2. Tunnel from the laptop

```bash
ssh -N -L 8000:127.0.0.1:8000 <user>@<gpu-host>
curl -s -H "Authorization: Bearer $KARTOGRAF_API_KEY" http://localhost:8000/v1/models
```

## 3. Run the evals

```bash
export KARTOGRAF_API_KEY=<the key from step 1>
.venv/bin/python run_evals.py --provider openai --base-url http://localhost:8000/v1 \
  --model Aleph-Alpha/Kolibri-1 --only en01 de09                # smoke test
.venv/bin/python run_evals.py --provider openai --base-url http://localhost:8000/v1 \
  --model Aleph-Alpha/Kolibri-1 \
  --extra-body '{"reasoning_effort": "low"}'
```

The run writes `evals/results/<date>_Aleph-Alpha--Kolibri-1.jsonl` and adds a row to
`evals/results/SUMMARY.md` next to the local models. `--extra-body` passes provider
options. `reasoning_effort` takes `none`, `low`, `medium` or `high`. The other models
run at temperature 0, and Kolibri does too unless `--extra-body` overrides it.

## 4. Use it in the web app

```bash
KARTOGRAF_PROVIDER=openai KARTOGRAF_BASE_URL=http://localhost:8000/v1 \
KARTOGRAF_MODELS=Aleph-Alpha/Kolibri-1 KARTOGRAF_API_KEY=<key> \
.venv/bin/python server.py
```
