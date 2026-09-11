# Deploying to the Predator

Windows, GTX 1060 6GB. Order matters: isolation is verified before the tunnel
goes public.

## 1. Prerequisites on the Predator

- NVIDIA driver on the Windows host (not inside WSL)
- WSL2 with Ubuntu
- Docker Desktop with the WSL2 backend and GPU support enabled
- Tailscale, logged in

Verify CUDA reaches WSL:
```bash
nvidia-smi
```

## 2. Clone and fetch the model

```bash
git clone <repo> ~/assistant && cd ~/assistant
./scripts/download-model.sh          # about 5GB, once
cp .env.example .env
```

Weights are gitignored. They live on the machine, mounted into the container.

## 3. Start llama-server

Batching flags are one system. Raising `--parallel` alone spreads the same
throughput across more queues: latency rises, aggregate does not.

```bash
llama-server -m models/Qwen3-8B-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8080 \
  --n-gpu-layers 28 \
  --ctx-size 8192 \
  --parallel 2 \
  --batch-size 512 --ubatch-size 128 \
  --cache-reuse 256
```

`--n-gpu-layers` is the number to tune first on a 6GB card. Raise until VRAM
spills, then back off one step. Layers that do not fit run on CPU at roughly a
third of the speed.

## 4. Run the app

```bash
docker compose up -d --build
```

## 5. Isolation verification (gate)

The tunnel stays off until every one of these passes. Record the output.

```bash
./scripts/verify-isolation.sh
```

Manually confirm as well:
- `nmap` the Predator from another machine on the LAN: no open ports
- Pull the network cable: the tunnel dies rather than failing open

## 6. Windows Firewall

Run in an elevated PowerShell. This is the rule that protects the other devices
on the network.

```powershell
New-NetFirewallRule -DisplayName "Block WSL to LAN" -Direction Outbound `
  -InterfaceAlias "vEthernet (WSL)" -Action Block `
  -RemoteAddress 10.0.0.0/8,192.168.0.0/16,172.16.0.0/12,169.254.0.0/16
```

Also disable file sharing on the host:
```powershell
Set-Service -Name LanmanServer -StartupType Disabled
Stop-Service -Name LanmanServer
```

## 7. Tunnel

Only after step 5 passes.

```bash
cloudflared tunnel login
cloudflared tunnel create assistant
cloudflared tunnel route dns assistant <hostname>
```

Put the token in `.env`, then `docker compose up -d cloudflared`.

## Updating

```bash
make deploy
```

Pulls, rebuilds on the Predator, health-checks, rolls back on failure. Source
is shipped and built there; the Mac is arm64 and the Predator is amd64, so
images do not transfer.
