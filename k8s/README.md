# Kubernetes Manifests — MediVoice

Validated against K8s 1.29 API. Tested with Minikube and GKE Autopilot.

## Prerequisites

- `kubectl` configured for your cluster
- `cert-manager` installed (for TLS — `kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml`)
- `ingress-nginx` controller installed
- Docker image pushed to `ghcr.io/<user>/medivoice-server` (see `../.github/workflows/docker.yml`)

## Apply

```bash
# 1. Create namespace first
kubectl apply -f k8s/namespace.yaml

# 2. Create secrets (never commit the real secret file)
kubectl create secret generic medivoice-secrets \
  --from-env-file=apps/server/.env.local \
  --namespace medivoice

# 3. Apply everything else
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/ingress.yaml

# Or apply the entire folder (namespace + secret must exist first)
kubectl apply -f k8s/
```

## Validate without a cluster (dry-run)

```bash
kubectl apply --dry-run=client -f k8s/
```

CI runs this check on every PR that touches `k8s/`.

## Scale

```bash
# Manual scale
kubectl scale deployment medivoice-server --replicas=5 -n medivoice

# HPA kicks in automatically at > 70% CPU across pods (min 2, max 10)
kubectl get hpa medivoice-server -n medivoice -w
```

## Image tag

Update `k8s/deployment.yaml` → `image:` to pin a specific version:

```yaml
image: ghcr.io/GITHUB_USER/medivoice-server:sha-abc1234
```

The CI workflow pushes both `:latest` and `:<git-sha>` tags.

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8000 | HTTP | FastAPI (REST + WebSocket) |
| 50051 | gRPC | Health / metrics / eval trigger |

## Readiness gate

The readinessProbe uses gRPC health protocol on :50051 (the `Check` RPC). A pod is only added to the Service load balancer after Postgres and Qdrant are reachable.

## Secrets management

Never commit `secret.yaml` with real values. In production, prefer an external secret operator:

- **AWS EKS**: External Secrets Operator + AWS Secrets Manager
- **GKE**: Workload Identity + Secret Manager
- **Self-hosted**: Vault Agent Injector

The `secret.yaml.example` file is a template showing the expected keys.
