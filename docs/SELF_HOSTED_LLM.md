# Self-hosted LLM boxes

Two EC2 boxes serve the app's AI features through vLLM. Exactly one is live at
a time, chosen by `SELF_HOSTED_LLM_TARGET`; the other can stay stopped.

| Target | Instance | GPU | Model (HF repo) | Served alias | Public endpoint | Dev tunnel |
|---|---|---|---|---|---|---|
| `ec2`  | g6.xlarge (`i-087a58e7a8ee2f11b`) | L4, 24 GB | `Qwen/Qwen3.5-4B`, online FP8 | `qwen-3.5-4b`  | `https://llm.g3electrical.com/v1`  | `localhost:8000` |
| `ec2b` | g7e.2xlarge | RTX PRO 6000 Blackwell, 96 GB | `Qwen/Qwen3.8-27B`, online FP8 | `qwen-3.8-27b` | `https://llm2.g3electrical.com/v1` | `localhost:8001` |
| `local` | this machine | | whatever `SELF_HOSTED_LLM_LOCAL_MODEL` says | | `SELF_HOSTED_LLM_LOCAL_BASE_URL` | |

Both models are vision-language models, so the bid file splitter (page images)
works on either. Qwen3.x thinks by default; both boxes disable thinking
server-side because the app expects plain JSON.

## The toggle

Each target is a base URL + API key + served model:

```
SELF_HOSTED_LLM_TARGET=ec2b            # local | ec2 | ec2b
SELF_HOSTED_LLM_EC2_BASE_URL=https://llm.g3electrical.com/v1
SELF_HOSTED_LLM_EC2_API_KEY=...
SELF_HOSTED_LLM_EC2_MODEL=qwen-3.5-4b
SELF_HOSTED_LLM_EC2B_BASE_URL=https://llm2.g3electrical.com/v1
SELF_HOSTED_LLM_EC2B_API_KEY=...
SELF_HOSTED_LLM_EC2B_MODEL=qwen-3.8-27b
```

Model resolution per feature (`app/services/llm.py`):

1. `SELF_HOSTED_<FEATURE>_MODEL` if set. The literal `off` disables the
   feature in self-hosted mode.
2. Otherwise the live target's `SELF_HOSTED_LLM_<TARGET>_MODEL`.
3. Empty in both places = the feature is off (the pre-toggle behaviour).

So with the per-feature lines left empty, switching box and model is the one
`SELF_HOSTED_LLM_TARGET` line. `FULL_SELF_HOSTED_LLMS_ENABLED=true` stays
strict: no third-party fallback while the chosen box is down. The sidebar
"Model status" indicator labels the live target (`EC2 box A` / `EC2 box B`)
and turns red with `model_missing` if the box serves a different alias than
the target's model line says.

Prod (Railway) flips the same way. Both `EC2B_*` lines must be set there
before `TARGET=ec2b`, or the backend refuses to boot. Rollback is
`TARGET=ec2` (or `FULL_SELF_HOSTED_LLMS_ENABLED=false`).

## From the Mac (`~/bin/bdr-llm`)

443 on both boxes is open to Railway's static IPs only, so the dev backend
reaches a box through an SSH tunnel on the `local` target:

```
bdr-llm up ec2b        # start the instance, wait for the model, tunnel localhost:8001
bdr-llm use ec2b       # rewrite bdr_be/.env: TARGET=local, LOCAL_BASE_URL=:8001, LOCAL_MODEL=qwen-3.8-27b
bdr-llm status all
bdr-llm logs ec2b
bdr-llm ssh ec2b
bdr-llm down ec2b      # stops the instance: if Railway is on ec2b, prod AI goes DOWN
```

Per-box hosts, keys and ports live in `~/.bdr-llm.conf`; instance IDs, regions
and API keys are read from `bdr_be/.env` (`SELF_HOSTED_LLM_<TARGET>_*`).

## Standing up a box (`ops/llm-box`)

`bdr-llm setup <box>` copies `ops/llm-box/` to the instance and runs
`setup.sh`, which installs vLLM in a venv, Caddy (Let's Encrypt HTTP-01),
pre-downloads the weights into `~/hf-cache` on the root EBS volume, installs
`vllm.service` (command line in `~/vllm/run.sh`, knobs in `~/vllm/vllm.env`)
and smoke-tests an exact reply with thinking off. Requirements the script
cannot create for you:

- AMI: **Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)**. The
  Neuron DLAMI has no GPU driver and Blackwell needs the open kernel modules.
- Root EBS 200 GB gp3 (Qwen3.8-27B is 55.6 GB of BF16 safetensors; the 1.9 TB
  instance-store NVMe is wiped on every stop so it is not used).
- Security group: 22 from the operator's IP, 80 from anywhere (ACME), 443 from
  Railway's static egress IPs. `sg-043db2d33e0ff7673` already has those rules.
- DNS A record for the domain pointing at the box's Elastic IP, before Caddy
  can get a certificate.

Tunables in `~/vllm/vllm.env` on the box: `QUANT_ARGS` (`--quantization fp8`
online, or empty for BF16, which fits on the 96 GB card at roughly half the
decode speed), `MAX_MODEL_LEN` (65536), `GPU_UTIL`, `MAX_NUM_SEQS`,
`IMAGES_PER_PROMPT` (the splitter sends 8 page images per call),
`REASONING_PARSER`, `EXTRA_ARGS`.

## Cost

g7e.2xlarge on-demand in us-west-2 is about $3.36/hr (roughly $2,450/month if
left running 24/7); g6.xlarge is about $0.80/hr. Whichever box Railway's
`TARGET` names must run 24/7; the other can be stopped (its Elastic IP then
bills a few dollars a month).
