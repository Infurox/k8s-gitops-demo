# Cloud native environment on Kubernetes

A demo web application (frontend and backend) deployed on Kubernetes with a focus
on scalability, security, monitoring and automated deployment via GitOps.

## Architecture

```
browser (http://aspecta.local)
   |
ingress-nginx controller
   |
   |-- /        Service frontend:80     Deployment frontend (2 pods, nginx)
   |
   |-- /api     Service backend:8000    Deployment backend (2 to 6 pods, FastAPI)
                                            |
                                        ConfigMap + Secret
```

Both Services are of type ClusterIP, so they are not reachable from outside. The
only entry point into the namespace is the Ingress, which routes by path. When
several paths match, the longest one wins, which is why `/api/items` ends up at
the backend.

The frontend is static nginx. The call to `/api/items` is made by the user's
browser, not by the frontend pod. That is why the frontend is not allowed to talk
to the backend, and the NetworkPolicy reflects this.

### Components

| Object | Purpose |
|---|---|
| Deployment frontend | 2 replicas, nginx-unprivileged, port 8080 |
| Deployment backend | FastAPI, port 8000, replica count managed by the HPA |
| Service frontend, backend | ClusterIP |
| Ingress | path-based routing, host aspecta.local |
| ConfigMap backend-config | APP_NAME, APP_ENV |
| Secret backend-secret | API_TOKEN protecting /api/admin |
| ServiceAccount, Role, RoleBinding | least required privileges |
| NetworkPolicy | default deny plus explicit allow rules |
| HorizontalPodAutoscaler | 2 to 6 replicas based on CPU, target 70 percent |
| ServiceMonitor | backend metrics scraping by Prometheus |
| PrometheusRule | three alerts |

## Requirements

- Linux with Docker Engine
- minikube 1.39 or newer
- kubectl, helm 3
- 6 GB of free memory for the cluster

## Installation from scratch

### 1. Cluster

```bash
minikube start --driver=docker --cni=calico --memory=6g --cpus=4
minikube addons enable ingress
minikube addons enable metrics-server
```

`--cni=calico` is essential. The default CNI in minikube accepts NetworkPolicy
objects but does not enforce them.

### 2. Monitoring

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --set kubeControllerManager.enabled=false \
  --set kubeScheduler.enabled=false \
  --set kubeEtcd.enabled=false
```

Control plane metrics are disabled on purpose. In minikube these components listen
only on the node's localhost, and managed clusters do not expose them at all.

### 3. ArgoCD

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server
```

`--server-side` is required because the ApplicationSet CRD exceeds the annotation
size limit under client-side apply.

### 4. Application

```bash
kubectl apply -f bootstrap/application.yaml
```

From this point on, Git is the source of truth. Nothing else is deployed by hand.

### 5. Browser access

```bash
echo "$(minikube ip) aspecta.local" | sudo tee -a /etc/hosts
```

## Usage

### Application

http://aspecta.local

| Endpoint | Description |
|---|---|
| GET /api/health | health check for probes |
| GET /api/info | values from the ConfigMap |
| GET /api/items | list of items |
| GET /api/admin | requires the X-API-Token header from the Secret |
| GET /metrics | Prometheus metrics, not exposed through the Ingress |

```bash
curl http://aspecta.local/api/items
curl -H "X-API-Token: demo-token-12345" http://aspecta.local/api/admin
```

### Tools

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090
```

| Tool | Address | Password |
|---|---|---|
| ArgoCD | https://localhost:8080 | command below |
| Grafana | http://localhost:3000 | command below |
| Prometheus | http://localhost:9090 | no password |

```bash
# ArgoCD (user admin)
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d

# Grafana (user admin)
kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d
```

### Deploying a change

```bash
git add .
git commit -m "description of the change"
git push
```

Nothing else is needed. The pipeline builds the images, writes the new tag into
`chart/values.yaml`, and ArgoCD deploys the change within three minutes.

## CI/CD

The workflow `.github/workflows/ci.yaml` runs on changes in `app/**` and:

1. builds the frontend and backend images
2. pushes them to GHCR tagged with the commit SHA
3. rewrites `chart/values.yaml` with the new tag and bumps the version in `chart/Chart.yaml`
4. commits the change back to the repository

The commit from step 4 changes only `chart/**`, so the pipeline does not trigger
itself. As a second safeguard, its message contains `[skip ci]`.

The tag is the commit hash, not `latest`. Every build has an unambiguous tag, a
revert is a return to a previous tag, and the contents of an image never change.

## GitOps

ArgoCD pulls changes from Git itself and pushes nothing into the cluster. That is
why the whole chain works even on a local cluster behind NAT, with no public IP
and no open ports. The only outbound traffic is `git fetch` to GitHub and
`docker pull` from GHCR.

`syncPolicy.automated` with `selfHeal: true` means that a manual change made
directly in the cluster is reverted to the state defined in Git. `prune: true`
deletes objects that have been removed from the repository.

The polling interval is three minutes. A webhook would shorten it to seconds, but
it would require an inbound connection from GitHub to the cluster, which is not
possible in this environment. A sync can be triggered manually with
`argocd app sync aspecta`.

## Security

### RBAC

The backend runs under its own ServiceAccount, which is allowed to read only one
specific ConfigMap. A second identity, `viewer`, has read-only access within the
namespace. Both are Roles, not ClusterRoles, because neither has any reason to see
anything outside its namespace.

```bash
kubectl auth can-i get configmap/backend-config -n aspecta --as=system:serviceaccount:aspecta:backend
kubectl auth can-i get secrets -n aspecta --as=system:serviceaccount:aspecta:backend
kubectl auth can-i create deployments -n aspecta --as=system:serviceaccount:aspecta:viewer
```

Expected result: `yes`, `no`, `no`.

`resourceNames` restricts access to a specific object, but it works only with
verbs that operate on a single object. It cannot be used with `list`, which is why
the backend can read its own ConfigMap but cannot request a list of all of them.

### NetworkPolicy

The namespace is in default deny mode in both directions. Only the following is
explicitly allowed:

- DNS to kube-system for all pods
- ingress to the frontend from the ingress-nginx namespace, port 8080
- ingress to the backend from the ingress-nginx and monitoring namespaces, port 8000

The rule for monitoring is necessary, otherwise Prometheus would lose its target
and scraping would stop working.

Verifying that enforcement is active:

```bash
kubectl -n aspecta exec deploy/frontend -- wget -qO- --timeout=3 http://backend:8000/api/items
```

It must end with a timeout. The name `backend` resolves, because DNS is allowed,
but the connection itself does not go through.

NetworkPolicies are additive. They have no ordering and no deny rule. The result
is the union of all allow rules that apply to a pod.

### Containers

Both images have a numeric `USER` in the Dockerfile, and the pods run with `runAsNonRoot: true`, `allowPrivilegeEscalation: false` and all capabilities dropped.

## Monitoring

ServiceMonitor and PrometheusRule carry the label `release: monitoring`. Without
it, Prometheus would silently ignore them, because it uses this label to select
which objects belong to it.

Alerts:

| Alert | Condition | Severity |
|---|---|---|
| BackendDown | scrape target failing for 2 minutes | critical |
| BackendPodRestarting | more than 2 restarts in 10 minutes | warning |
| BackendMaxedOut | HPA at maximum replicas for 10 minutes | warning |

## Scaling

The HPA scales the backend between 2 and 6 replicas at a target of 70 percent CPU.
The percentage is calculated from `resources.requests.cpu`, not from the limit or
from the node's capacity. Without configured requests, the HPA would have nothing
to calculate from.

Verification:

```bash
kubectl -n aspecta get hpa -w
```

and in another terminal:

```bash
while true; do curl -s http://aspecta.local/api/items > /dev/null; done
```

It scales up quickly and scales down only after five minutes because of the
stabilization window.

## Repository structure

```
app/backend/          FastAPI application and Dockerfile
app/frontend/         static page, nginx configuration and Dockerfile
chart/                Helm chart with all Kubernetes objects
bootstrap/            ArgoCD Application, the only thing deployed by hand
.github/workflows/    CI pipeline
```

## Simplifications

Platform components (ArgoCD, ingress-nginx, metrics-server, monitoring stack) are
deployed imperatively as part of the cluster bootstrap, because ArgoCD has to
exist before it can manage anything else. Everything above this layer is declared
in this repository and deployed by ArgoCD.

The Secret is stored in the repository in plain text. Base64 in Kubernetes is not
encryption, it is only encoding. A real deployment would use sealed-secrets,
external-secrets or Vault.

The chart templates hard-code the namespace instead of using `.Release.Namespace`.
The chart can therefore only be deployed into the `aspecta` namespace.

Control plane metrics are disabled, see the Installation section.
