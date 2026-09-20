# Cloud native prostredie v Kubernetes

Demo webová aplikácia (frontend a backend) nasadená v Kubernetes s dôrazom na
škálovateľnosť, bezpečnosť, monitoring a automatizované nasadzovanie cez GitOps.

## Architektúra

```
prehliadač (http://aspecta.local)
   |
ingress-nginx controller
   |
   |-- /        Service frontend:80     Deployment frontend (2 pody, nginx)
   |
   |-- /api     Service backend:8000    Deployment backend (2 až 6 podov, FastAPI)
                                            |
                                        ConfigMap + Secret
```

Obe Service sú typu ClusterIP, teda zvonku neprístupné. Jediný vstup do namespace
je cez Ingress, ktorý smeruje podľa cesty. Pri zhode viacerých ciest vyhráva
najdlhšia, preto `/api/items` končí na backende.

Frontend je statický nginx. Volanie `/api/items` robí prehliadač používateľa,
nie pod frontendu. Preto frontend nemá povolené hovoriť na backend a NetworkPolicy
to odráža.

### Komponenty

| Objekt | Účel |
|---|---|
| Deployment frontend | 2 repliky, nginx-unprivileged, port 8080 |
| Deployment backend | FastAPI, port 8000, počet replík riadi HPA |
| Service frontend, backend | ClusterIP |
| Ingress | smerovanie podľa cesty, host aspecta.local |
| ConfigMap backend-config | APP_NAME, APP_ENV |
| Secret backend-secret | API_TOKEN chrániaci /api/admin |
| ServiceAccount, Role, RoleBinding | najmenšie potrebné oprávnenia |
| NetworkPolicy | default deny plus explicitné povolenia |
| HorizontalPodAutoscaler | 2 až 6 replík podľa CPU, cieľ 70 percent |
| ServiceMonitor | zber metrík backendu Prometheusom |
| PrometheusRule | tri alerty |

## Požiadavky

- Linux s Docker Engine
- minikube 1.39 alebo novší
- kubectl, helm 3
- 6 GB voľnej pamäte pre klaster

## Inštalácia od nuly

### 1. Klaster

```bash
minikube start --driver=docker --cni=calico --memory=6g --cpus=4
minikube addons enable ingress
minikube addons enable metrics-server
```

`--cni=calico` je podstatné. Predvolené CNI v minikube objekty NetworkPolicy
prijme, ale neenforcuje ich.

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

Metriky control plane sú vypnuté zámerne. V minikube tieto komponenty počúvajú
len na localhost node-u a spravované klastre ich nevystavujú vôbec.

### 3. ArgoCD

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server
```

`--server-side` je potrebné, CRD ApplicationSet prekračuje limit veľkosti
anotácie pri client-side apply.

### 4. Aplikácia

```bash
kubectl apply -f bootstrap/application.yaml
```

Od tohto momentu je zdrojom pravdy Git. Nič ďalšie sa ručne nenasadzuje.

### 5. Prístup z prehliadača

```bash
echo "$(minikube ip) aspecta.local" | sudo tee -a /etc/hosts
```

## Používanie

### Aplikácia

http://aspecta.local

| Endpoint | Popis |
|---|---|
| GET /api/health | health check pre probes |
| GET /api/info | hodnoty z ConfigMapy |
| GET /api/items | zoznam položiek |
| GET /api/admin | vyžaduje hlavičku X-API-Token zo Secretu |
| GET /metrics | Prometheus metriky, nie je vystavený cez Ingress |

```bash
curl http://aspecta.local/api/items
curl -H "X-API-Token: demo-token-12345" http://aspecta.local/api/admin
```

### Nástroje

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090
```

| Nástroj | Adresa | Heslo |
|---|---|---|
| ArgoCD | https://localhost:8080 | príkaz nižšie |
| Grafana | http://localhost:3000 | príkaz nižšie |
| Prometheus | http://localhost:9090 | bez hesla |

```bash
# ArgoCD (používateľ admin)
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d

# Grafana (používateľ admin)
kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d
```

### Nasadenie zmeny

```bash
git add .
git commit -m "popis zmeny"
git push
```

Ďalej sa nerobí nič. Pipeline zbuilduje imagey, zapíše nový tag do
`chart/values.yaml` a ArgoCD zmenu do troch minút nasadí.

## CI/CD

Workflow `.github/workflows/ci.yaml` sa spúšťa pri zmene v `app/**` a:

1. zbuilduje imagey frontendu a backendu
2. pošle ich do GHCR s tagom rovným SHA commitu
3. prepíše `chart/values.yaml` novým tagom a bumpne verziu v `chart/Chart.yaml`
4. commitne zmenu späť do repozitára

Commit z bodu 4 mení iba `chart/**`, takže pipeline nespúšťa sám seba. Ako druhá
poistka je v jeho správe `[skip ci]`.

Tagom je hash commitu, nie `latest`. Každý build má nezameniteľný tag, revert je
návrat na predchádzajúci tag a obsah imagu sa už nikdy nezmení.

## GitOps

ArgoCD si zmeny sám sťahuje z Gitu, nič netlačí dovnútra klastra. Preto celý
reťazec funguje aj na lokálnom klastri za NATom bez verejnej IP a bez otvorených
portov. Jediná prevádzka smerom von je `git fetch` na GitHub a `docker pull` z GHCR.

`syncPolicy.automated` so `selfHeal: true` znamená, že ručná zmena priamo
v klastri sa vráti do stavu podľa Gitu. `prune: true` maže objekty odstránené
z repozitára.

Interval dotazovania je tri minúty. Webhook by ho skrátil na sekundy, ale
vyžadoval by prichádzajúce spojenie z GitHubu do klastra, čo v tomto prostredí
nie je možné. Manuálne sa sync vyvolá cez `argocd app sync aspecta`.

## Bezpečnosť

### RBAC

Backend beží pod vlastným ServiceAccountom, ktorý smie iba čítať jednu konkrétnu
ConfigMapu. Druhá identita `viewer` má read-only prístup v rámci namespace.
Obe sú Role, nie ClusterRole, pretože ani jedna nemá dôvod vidieť nič mimo
svojho namespace.

```bash
kubectl auth can-i get configmap/backend-config -n aspecta --as=system:serviceaccount:aspecta:backend
kubectl auth can-i get secrets -n aspecta --as=system:serviceaccount:aspecta:backend
kubectl auth can-i create deployments -n aspecta --as=system:serviceaccount:aspecta:viewer
```

Očakávaný výsledok: `yes`, `no`, `no`.

`resourceNames` obmedzuje prístup na konkrétny objekt, ale funguje len pri
slovesách nad jedným objektom. Na `list` sa použiť nedá, preto backend vie
prečítať svoju ConfigMapu, ale nevie si vypýtať zoznam všetkých.

### NetworkPolicy

Namespace je v režime default deny v oboch smeroch. Explicitne je povolené iba:

- DNS na kube-system pre všetky pody
- vstup na frontend z namespace ingress-nginx, port 8080
- vstup na backend z namespace ingress-nginx a z namespace monitoring, port 8000

Povolenie pre monitoring je nutné, inak by Prometheus stratil cieľ a scrape by
prestal chodiť.

Overenie, že enforcement beží:

```bash
kubectl -n aspecta exec deploy/frontend -- wget -qO- --timeout=3 http://backend:8000/api/items
```

Musí skončiť timeoutom. Meno `backend` sa pritom preloží, DNS je povolené,
neprejde až samotné spojenie.

NetworkPolicy sú aditívne, nemajú poradie ani zamietacie pravidlo. Výsledok je
zjednotenie všetkých povolení, ktoré sa na pod vzťahujú.

### Kontajnery

Oba imagey majú v Dockerfile numerický `USER`, pody bežia s `runAsNonRoot: true`,
`allowPrivilegeEscalation: false` a so zahodenými capabilities.

## Monitoring

ServiceMonitor a PrometheusRule nesú label `release: monitoring`. Bez neho by ich
Prometheus ticho ignoroval, pretože si podľa tohto labelu vyberá, ktoré objekty
sú jeho.

Alerty:

| Alert | Podmienka | Závažnosť |
|---|---|---|
| BackendDown | scrape cieľa zlyháva 2 minúty | critical |
| BackendPodRestarting | viac ako 2 reštarty za 10 minút | warning |
| BackendMaxedOut | HPA beží 10 minút na maxime replík | warning |

## Škálovanie

HPA škáluje backend medzi 2 a 6 replikami pri cieli 70 percent CPU. Percento sa
počíta z `resources.requests.cpu`, nie z limitu ani z kapacity node-u. Bez
nastavených requestov by HPA nemal z čoho počítať.

Overenie:

```bash
kubectl -n aspecta get hpa -w
```

a v inom termináli:

```bash
while true; do curl -s http://aspecta.local/api/items > /dev/null; done
```

Nahor reaguje rýchlo, nadol až po piatich minútach kvôli stabilizačnému oknu.

## Štruktúra repozitára

```
app/backend/          FastAPI aplikácia a Dockerfile
app/frontend/         statická stránka, nginx konfigurácia a Dockerfile
chart/                Helm chart so všetkými Kubernetes objektmi
bootstrap/            ArgoCD Application, jediná vec nasadzovaná ručne
.github/workflows/    CI pipeline
```

## Zjednodušenia

Platformové komponenty (ArgoCD, ingress-nginx, metrics-server, monitoring stack)
sú nasadené imperatívne ako súčasť bootstrapu klastra, pretože ArgoCD musí
existovať skôr, než môže spravovať čokoľvek iné. Všetko nad touto vrstvou je
deklarované v tomto repozitári a nasadzuje ho ArgoCD.

Secret je v repozitári v čistom texte. Base64 v Kubernetes nie je šifrovanie,
je to len kódovanie. V reálnom nasadení by sa použil sealed-secrets,
external-secrets alebo Vault.

Šablóny chartu majú namespace uvedený natvrdo namiesto `.Release.Namespace`.
Chart sa preto nasadzuje len do namespace `aspecta`.

Metriky control plane sú vypnuté, viď sekcia Inštalácia.
