#!/usr/bin/env bash
# setup.sh - one-shot install of vLLM + Caddy on a fresh EC2 GPU box.
#
# Target AMI: "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)".
# It ships the open NVIDIA kernel modules (required for Blackwell) and the CUDA
# toolkit. NOT the Neuron DLAMI (no GPU driver) - the script refuses to run
# without a working nvidia-smi.
#
# Run from the Mac with `bdr-llm setup <box>`; parameters arrive as env vars:
#   HF_MODEL        Hugging Face repo, e.g. Qwen/Qwen3.8-27B          (required)
#   SERVED_NAME     stable alias the app uses, e.g. qwen-3.8-27b      (required)
#   DOMAIN          public hostname Caddy terminates TLS for           (required)
#   VLLM_API_KEY    bearer token vLLM enforces on /v1/*                (required)
#   QUANT           fp8 (online, default) | none (BF16)
#   MAX_MODEL_LEN   context window tokens (default 65536)
#   GPU_UTIL        --gpu-memory-utilization (default 0.90)
#   MAX_NUM_SEQS    concurrent sequences (default 16)
#   IMAGES_PER_PROMPT  --limit-mm-per-prompt image cap (default 16; bid splitter sends 8 pages/call)
#   REASONING_PARSER   default qwen3 (used only as a safety net; thinking is disabled server-side)
#   VLLM_VERSION    pip spec, default "vllm" (latest); e.g. "vllm==0.28.1" to pin
#   HF_TOKEN        only if the repo is gated (Qwen3.8 is Apache-2.0, not gated)
#
# Idempotent: re-running updates the env/unit/Caddyfile and restarts services.
set -euo pipefail

: "${HF_MODEL:?HF_MODEL is required}" "${SERVED_NAME:?SERVED_NAME is required}"
: "${DOMAIN:?DOMAIN is required}" "${VLLM_API_KEY:?VLLM_API_KEY is required}"
QUANT="${QUANT:-fp8}"; MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"; GPU_UTIL="${GPU_UTIL:-0.90}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"; IMAGES_PER_PROMPT="${IMAGES_PER_PROMPT:-16}"
REASONING_PARSER="${REASONING_PARSER:-qwen3}"; VLLM_VERSION="${VLLM_VERSION:-vllm}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
APT="sudo apt-get -o DPkg::Lock::Timeout=900 -y -q"

# 0. GPU sanity (the AMI trap: Neuron DLAMI has the card in lspci but no driver)
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv \
  || die "nvidia-smi is missing or failing - wrong AMI? Launch from the Deep Learning Base OSS Nvidia Driver GPU AMI."

# 1. OS packages + Caddy repo
say "apt: base packages + Caddy"
$APT update
$APT install python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https gnupg
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  $APT update
  $APT install caddy
fi

# 2. vLLM in its own venv; weights cached on the root EBS volume (persistent
#    across stop/start; the instance-store NVMe is wiped every stop).
say "vLLM venv ($VLLM_VERSION)"
mkdir -p "$HOME/vllm" "$HOME/hf-cache"
[ -x "$HOME/vllm/venv/bin/python" ] || python3 -m venv "$HOME/vllm/venv"
"$HOME/vllm/venv/bin/pip" install -q -U pip
"$HOME/vllm/venv/bin/pip" install -q -U "$VLLM_VERSION" huggingface_hub aiohttp
"$HOME/vllm/venv/bin/python" -c 'import vllm, torch; print("vllm", vllm.__version__, "torch", torch.__version__, "cuda", torch.version.cuda, "sm", torch.cuda.get_device_capability())'

# 3. Secrets + tunables for the unit (0600). VLLM_API_KEY is read by vLLM from
#    the environment so the key never appears on a command line.
say "writing ~/vllm/vllm.env"
umask 077
cat > "$HOME/vllm/vllm.env" <<ENV
VLLM_API_KEY=$VLLM_API_KEY
HF_HOME=$HOME/hf-cache
HF_HUB_ENABLE_HF_TRANSFER=0
${HF_TOKEN:+HF_TOKEN=$HF_TOKEN}
# FlashInfer's JIT sampler needs nvcc; harmless to disable if the toolkit is present.
VLLM_USE_FLASHINFER_SAMPLER=0
HF_MODEL=$HF_MODEL
SERVED_NAME=$SERVED_NAME
MAX_MODEL_LEN=$MAX_MODEL_LEN
GPU_UTIL=$GPU_UTIL
MAX_NUM_SEQS=$MAX_NUM_SEQS
IMAGES_PER_PROMPT=$IMAGES_PER_PROMPT
REASONING_PARSER=$REASONING_PARSER
QUANT_ARGS="$([ "$QUANT" = none ] && echo "" || echo "--quantization $QUANT")"
ENV
umask 022

# 4. Pre-download the weights in the foreground so the first service start
#    does not sit in "activating" for the length of a 55 GB download.
say "downloading $HF_MODEL into $HOME/hf-cache (skips files already present)"
HF_HOME="$HOME/hf-cache" env ${HF_TOKEN:+HF_TOKEN=$HF_TOKEN} "$HOME/vllm/venv/bin/python" - "$HF_MODEL" <<'PY'
import sys
from huggingface_hub import snapshot_download
print(snapshot_download(sys.argv[1]))
PY
df -h "$HOME" | tail -1

# 5. systemd unit + Caddy
say "installing vllm.service and the Caddyfile"
install -m 755 "$(dirname "$0")/run.sh" "$HOME/vllm/run.sh"
sed -e "s|__HOME__|$HOME|g" -e "s|__USER__|$USER|g" "$(dirname "$0")/vllm.service" | sudo tee /etc/systemd/system/vllm.service >/dev/null
sed -e "s|__DOMAIN__|$DOMAIN|g" "$(dirname "$0")/Caddyfile" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable vllm.service caddy >/dev/null
sudo systemctl restart caddy
sudo systemctl restart vllm.service

# 6. Wait for the model (cold start of a 27B with online FP8 quant is minutes)
say "waiting for vLLM on 127.0.0.1:8000 (Ctrl-C is safe; the unit keeps loading)"
for i in $(seq 1 180); do
  if curl -fsS -m 5 -H "Authorization: Bearer $VLLM_API_KEY" http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
    say "model is up:"; curl -sS -H "Authorization: Bearer $VLLM_API_KEY" http://127.0.0.1:8000/v1/models | python3 -c 'import sys,json; [print("   -", m["id"]) for m in json.load(sys.stdin)["data"]]'
    break
  fi
  printf '\r    still loading... %ds' $((i*10)); sleep 10
  [ "$i" = 180 ] && { printf '\n'; die "vLLM did not come up in 30 min: sudo journalctl -u vllm.service -n 200"; }
done

# 7. Prove the two things the app relies on: thinking is off, JSON mode works.
say "smoke test (exact reply, thinking disabled)"
curl -sS -m 120 http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$SERVED_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: READY\"}],\"max_tokens\":16,\"temperature\":0}" \
  | python3 -c 'import sys,json; r=json.load(sys.stdin); c=r["choices"][0]; print("   content:", repr(c["message"]["content"]), "| finish:", c["finish_reason"], "| tokens:", r["usage"]["completion_tokens"])'
say "done. Caddy will obtain the certificate for https://$DOMAIN once DNS points here and port 80 is open."
say "check: sudo journalctl -u caddy -n 30 --no-pager"
