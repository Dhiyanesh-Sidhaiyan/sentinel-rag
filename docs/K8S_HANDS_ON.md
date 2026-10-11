# Kubernetes Hands-On — Learn K8s with Your Own Project

> **Who this is for:** you know Docker a little, and Kubernetes not at all.
> **How it works:** every concept is explained in plain words, then shown on **your real cluster**
> (Docker Desktop, 4 nodes) running **your real project** (Sentinel RAG). Every command and output in this
> guide was run on your machine.
>
> **Golden rule:** don't just read. Type every command. Predict the output *before* you press Enter.

---

## Part 0 — Before you start

### What you need running
1. Docker Desktop → Settings → Kubernetes → **enabled** (yours: 4-node "kind" cluster).
2. The project deployed:
```bash
cd /Users/dsidhaiyan/Project/sentinel-rag && make k8s-up
```

### One setting already made for you
Your default namespace is set to `sentinel-rag`, so you can type `kubectl get pods` instead of
`kubectl get pods -n sentinel-rag`. (It was set with
`kubectl config set-context --current --namespace=sentinel-rag`.)

### How to read commands in this guide
- `kubectl <verb> <resource> <name> [flags]` — e.g. `kubectl describe pod postgres-0`
- Flags for kubectl (like `-n`, `-o wide`) go **before** `--`. Anything **after** `--` runs inside the container.
- Don't type `<...>` literally — replace it with a real name.

---

## Part 1 — The one idea that explains all of Kubernetes

### The problem Kubernetes solves
With Docker alone, *you* are the operator: you start containers, restart them when they crash, move them when a
machine dies, add more when traffic grows, and wire them together. That doesn't scale past a few containers.

### Kubernetes = "desired state" + robots that never sleep
You write a **wish list** in YAML ("I want 3 copies of my API, each with 384Mi memory, reachable on port 80").
You hand it to Kubernetes. From then on, small programs called **controllers** run a loop forever:

```
        ┌──────────────────────────────────────────────┐
        │  1. Read desired state (your YAML)           │
        │  2. Look at actual state (what's running)    │
        │  3. Different?  -> take action to fix it     │
        │  4. Sleep a moment, repeat forever           │
        └──────────────────────────────────────────────┘
```

This is called the **reconciliation loop**. Almost every "magic" thing Kubernetes does is this loop:

| You see | What actually happened |
|---|---|
| You delete a pod and a new one appears | Desired = 1 pod, actual = 0 → controller creates one |
| You change the image and pods get replaced | Desired image ≠ running image → controller rolls out new pods |
| A node dies and pods move | Desired pods on healthy nodes ≠ actual → rescheduled |
| Traffic spikes and pods are added | The autoscaler changes desired from 1 → 3 → controller creates 2 |

**Interview line:** *"Kubernetes is declarative: you describe the desired state, and controllers continuously
reconcile actual state toward it."* Memorize this sentence — it answers half of beginner K8s questions.

### Declarative vs imperative
- **Imperative** = "do this now": `kubectl scale deploy/sentinel-rag --replicas=3`
- **Declarative** = "this is how it should be": the YAML in `k8s/` + `kubectl apply`

Production uses declarative (YAML in Git = source of truth). Imperative commands are for learning and emergencies —
and the next `make k8s-up` will **overwrite** them (you'll see this in Lab 6).

---

## Part 2 — Anatomy of YOUR cluster

### 2.1 Cluster and nodes

```bash
kubectl get nodes -o wide
```
Your output:
```
NAME                    STATUS   ROLES           VERSION   INTERNAL-IP
desktop-control-plane   Ready    control-plane   v1.36.1   172.20.0.2
desktop-worker          Ready    <none>          v1.36.1   172.20.0.3
desktop-worker2         Ready    <none>          v1.36.1   172.20.0.5
desktop-worker3         Ready    <none>          v1.36.1   172.20.0.4
```

- **Cluster** = the whole system: a group of machines managed as one.
- **Node** = one machine (VM or physical server) in the cluster. In Docker Desktop's "kind" mode, each node is
  actually a container pretending to be a machine — perfect for learning, same behavior as real servers.
- **Control-plane node** = the "brain". **Worker nodes** = the "muscle" where your apps run.

Your control plane has a **taint** so your apps never run there:
```bash
kubectl describe node desktop-control-plane | grep -i taints
```
```
Taints:             node-role.kubernetes.io/control-plane:NoSchedule
```
A **taint** is a "keep out" sign on a node. Only pods with a matching **toleration** can be scheduled there.

### 2.2 What's inside the brain (control plane)

```bash
kubectl get pods -n kube-system -o wide
```
You'll see these system components — **know all four for interviews**:

| Component | Job | Analogy |
|---|---|---|
| `kube-apiserver` | The front door. Every `kubectl` command, every controller talks only to it | Reception desk |
| `etcd` | Database storing the whole cluster state (all your YAML, live status) | The filing cabinet |
| `kube-scheduler` | Decides **which node** a new pod should run on | Seating planner |
| `kube-controller-manager` | Runs the reconciliation loops (Deployments, ReplicaSets, nodes...) | Managers checking work |
| `coredns` | Cluster DNS: turns the name `postgres` into an IP | Phone book |

### 2.3 What runs on every worker node

| Component | Job |
|---|---|
| `kubelet` | Agent on each node: starts/stops containers, runs health probes, reports status |
| container runtime (`containerd`) | Actually runs containers (like Docker engine) |
| `kube-proxy` | Programs networking rules so Service IPs route to pods |
| `kindnet` | The network plugin (CNI): gives pods IPs and **enforces NetworkPolicies** |

You saw `kindnet-*` and `kube-proxy-*` once **per node** — they're **DaemonSets** (a DaemonSet = "run one copy on
every node").

### 2.4 What happens when you run `kubectl apply`

```
kubectl apply -f deployment.yaml
   │
   ▼
kube-apiserver ──validates, checks permissions & Pod Security──► saves to etcd
   │
   ▼
Deployment controller: "need a ReplicaSet"  ──► ReplicaSet controller: "need 1 pod"
   │
   ▼
kube-scheduler: "desktop-worker has room" ──► assigns pod to node
   │
   ▼
kubelet on desktop-worker: pulls image, starts container, runs probes, reports "Running/Ready"
```

### 2.5 The project's full picture inside the cluster

```
                         Namespace: sentinel-rag
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                                                                          │
  │  Service sentinel-rag (10.96.29.200:80)                                  │
  │        │  load-balances to all READY api pods                            │
  │        ▼                                                                 │
  │  Deployment sentinel-rag ─► ReplicaSet ─► Pod(s)  [FastAPI + LangGraph]  │
  │        │ env from ConfigMap sentinel-rag-config + Secret sentinel-rag-…  │
  │        │                                                                 │
  │        ├──► Service postgres:5432 ─► StatefulSet postgres ─► postgres-0 ─► PVC 5Gi
  │        ├──► Service qdrant:6333   ─► StatefulSet qdrant   ─► qdrant-0   ─► PVC 10Gi
  │        └──► Service valkey:6379   ─► Deployment valkey    ─► valkey-…   (no disk)
  │                                                                          │
  │  HPA (scales api 1..3)   PDB   NetworkPolicies (firewall)   Ingress      │
  └──────────────────────────────────────────────────────────────────────────┘
```

See it all at once:
```bash
kubectl get all,cm,secret,pvc,hpa,pdb,netpol
```

---

## Part 3 — Every Kubernetes object, explained with your project

Each section: **What → Why → Your YAML → Commands → Interview line.**

### 3.1 Namespace — a folder for resources

**What:** a logical partition inside a cluster. Names must be unique *within* a namespace.
**Why:** separate teams/apps/environments, apply security rules and quotas per namespace, delete everything at once.

Your YAML ([k8s/base/namespace.yaml](../k8s/base/namespace.yaml)):
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: sentinel-rag
  labels:
    pod-security.kubernetes.io/enforce: restricted   # strictest Pod Security level (see 3.15)
```
```bash
kubectl get namespaces
kubectl get pods -A          # -A = all namespaces
```
> This is why `kubectl get pods` "couldn't find" your pods earlier — they're in `sentinel-rag`, not `default`.

**Interview:** *"Namespaces provide scoping for names, RBAC, quotas and policies. They are not a hard security
boundary by themselves — you add NetworkPolicies and RBAC for isolation."*

### 3.2 Pod — the smallest unit

**What:** one or more containers that share an IP address and storage, scheduled together on one node. Usually
1 container = 1 pod.
**Key fact:** pods are **mortal and replaceable**. They get a new name and new IP when recreated. You almost never
create pods directly — a controller (Deployment/StatefulSet) creates them for you.

```bash
kubectl get pods -o wide
```
```
NAME                            READY   STATUS    RESTARTS   IP           NODE
postgres-0                      1/1     Running   0          ...          desktop-worker
qdrant-0                        1/1     Running   0          ...          desktop-worker2
sentinel-rag-666d4d954d-bz4v5   1/1     Running   0          ...          desktop-worker
valkey-5b9d54755b-g2dx8         1/1     Running   0          ...          desktop-worker2
```

Reading a pod name: `sentinel-rag-666d4d954d-bz4v5`
- `sentinel-rag` → the Deployment
- `666d4d954d` → the ReplicaSet (a hash of the pod template = **one version** of your app)
- `bz4v5` → random pod suffix

`READY 1/1` = 1 of 1 containers passed the readiness probe. `RESTARTS` = how often the container crashed and was
restarted by kubelet.

Look inside:
```bash
kubectl describe pod postgres-0          # full details + Events at the bottom (read Events first!)
kubectl logs deploy/sentinel-rag         # app logs (JSON lines from structlog)
kubectl exec -it deploy/sentinel-rag -- sh    # shell inside the API container (type exit to leave)
```

**Pod phases:** `Pending` (waiting for a node / image) → `Running` → `Succeeded` / `Failed`.
`CrashLoopBackOff` is not a phase — it means "crashed, kubelet is waiting longer and longer before retrying".

### 3.3 ReplicaSet — "keep N identical pods running"

**What:** a controller that ensures exactly N copies of a pod template exist.
You never create these yourself — Deployments create one ReplicaSet **per version**.

```bash
kubectl get rs -l app.kubernetes.io/component=api \
  -o custom-columns='REPLICASET:.metadata.name,PODS:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image'
```
Your real output — your deploy history:
```
REPLICASET                PODS   IMAGE
sentinel-rag-dfb5ffb6d    0      sentinel-rag:local-b31976754df1   <- docs page fix
sentinel-rag-c7ff6d56     0      sentinel-rag:local-b160e48af5f9   <- Authorize button change
sentinel-rag-666d4d954d   1      sentinel-rag:local-6e7111b12388   <- current version
```
Old ReplicaSets are kept at 0 pods so `kubectl rollout undo` can go back **instantly**.

### 3.4 Deployment — for stateless apps (your API, Valkey)

**What:** manages ReplicaSets to give you **rolling updates** and **rollbacks**.
**Use for:** anything where every copy is identical and disposable (web servers, APIs, workers).

Key parts of your [deployment.yaml](../k8s/base/deployment.yaml):
```yaml
kind: Deployment
spec:
  replicas: 3                       # dev overlay patches this to 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%                 # may create extra pods during an update
      maxUnavailable: 0             # never go below desired count -> zero downtime
  selector:
    matchLabels: { app.kubernetes.io/name: sentinel-rag, app.kubernetes.io/component: api }
  template:                         # <- the POD template. Changing anything here = new version
    metadata:
      labels: { app.kubernetes.io/name: sentinel-rag, app.kubernetes.io/component: api }
    spec:
      containers:
        - name: api
          image: sentinel-rag
```

**Labels & selectors — the glue of Kubernetes.** Labels are key/value tags on objects. A **selector** finds
objects by label. The Deployment finds *its* pods by `app.kubernetes.io/name=sentinel-rag` +
`component=api`; the Service finds pods to send traffic to the **same way**. Nothing is linked by name — only labels.

```bash
kubectl get pods --show-labels
kubectl get pods -l app.kubernetes.io/component=api       # select by label
```

**Rolling update, step by step** (what happened every time I deployed your code changes):
```
desired image changes  ->  new ReplicaSet created
   new pod starts  ->  readinessProbe passes  ->  Service starts sending it traffic
   old pod gets SIGTERM  ->  preStop sleep 10s  ->  graceful shutdown  ->  deleted
```
```bash
kubectl rollout status deploy/sentinel-rag
kubectl rollout history deploy/sentinel-rag
kubectl rollout undo deploy/sentinel-rag           # back to the previous version
kubectl rollout restart deploy/sentinel-rag        # new pods, SAME image (e.g. after config change)
```

### 3.5 Service — a stable address for changing pods

**Problem:** pods die and get new IPs. How does the API find Postgres?
**What:** a Service gives a **stable virtual IP + DNS name** and load-balances to all **ready** pods matching its selector.

```bash
kubectl get svc
```
```
NAME           TYPE        CLUSTER-IP      PORT(S)
postgres       ClusterIP   10.96.131.28    5432/TCP
qdrant         ClusterIP   10.96.66.82     6333/TCP,6334/TCP
sentinel-rag   ClusterIP   10.96.29.200    80/TCP
valkey         ClusterIP   10.96.158.198   6379/TCP
```
Your [service.yaml](../k8s/base/service.yaml):
```yaml
kind: Service
spec:
  type: ClusterIP
  selector: { app.kubernetes.io/name: sentinel-rag, app.kubernetes.io/component: api }
  ports:
    - { name: http, port: 80, targetPort: http }   # Service port 80 -> container port "http" (8000)
```

**DNS — how your code connects to `postgres`.** The API's config says `QDRANT_URL=http://qdrant:6333` and
`postgres@postgres:5432`. CoreDNS resolves those names to the Service IP. Proven on your cluster:
```bash
kubectl exec deploy/sentinel-rag -- python -c "import socket;print(socket.gethostbyname('postgres'))"
```
```
10.96.131.28
```
Full DNS name: `<service>.<namespace>.svc.cluster.local` → `postgres.sentinel-rag.svc.cluster.local`.
Short name `postgres` works inside the same namespace.

**Which pods are behind a Service right now?**
```bash
kubectl get endpointslices -l kubernetes.io/service-name=sentinel-rag \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"  ready="}{.conditions.ready}{"\n"}{end}'
```
```
10.244.3.6  ready=true
10.244.1.9  ready=true
```
Every matching pod is listed, but traffic goes **only** to endpoints with `ready=true`. That's how readiness
probes take broken pods out of rotation without killing them.

**Service types:**

| Type | Reachable from | Used for |
|---|---|---|
| `ClusterIP` (yours) | inside the cluster only | internal services — the default and most common |
| `NodePort` | `<any node IP>:30000-32767` | quick demos, rarely in production |
| `LoadBalancer` | the internet, via a cloud load balancer (AWS NLB/ALB) | exposing services in the cloud |
| Headless (`clusterIP: None`) | DNS returns pod IPs directly | StatefulSet peer discovery (e.g. DB clusters) |

**port-forward** — your laptop's tunnel into the cluster (dev only):
```bash
kubectl port-forward svc/sentinel-rag 8000:80     # localhost:8000 -> Service port 80
```
Note: port-forward attaches to **one pod**. If that pod is replaced (every deploy), restart the port-forward.

### 3.6 ConfigMap — non-secret configuration

**What:** key/value config stored in the cluster, injected into pods as env vars or files.
**Why:** the **same image** runs in dev and prod; only config changes (12-factor app).

```bash
kubectl get configmap sentinel-rag-config -o yaml
```
Your pod loads it all as env vars ([deployment.yaml](../k8s/base/deployment.yaml)):
```yaml
envFrom:
  - configMapRef: { name: sentinel-rag-config }   # every key becomes an env var: LOG_LEVEL=DEBUG ...
  - secretRef:    { name: sentinel-rag-secrets }
```
⚠️ **Gotcha:** env vars are read when the container **starts**. Editing a ConfigMap does **not** change running
pods — you must `kubectl rollout restart` (Lab 9 proves it). Kustomize's `configMapGenerator` can add a hash
suffix to the name so a config change forces a rollout automatically — your Secret uses this.

### 3.7 Secret — sensitive configuration

```bash
kubectl get secrets
kubectl describe $(kubectl get secret -o name | grep sentinel-rag-secrets)
```
`describe` shows **key names and sizes only**, never values:
```
Data
====
API_KEYS_SHA256:    ... bytes
POSTGRES_DSN:       ... bytes
POSTGRES_PASSWORD:  ... bytes
QDRANT_API_KEY:     ... bytes
REDIS_URL:          ... bytes
```
Secret name is `sentinel-rag-secrets-kkmmf5kgbt` — the **hash suffix** comes from Kustomize's `secretGenerator`.
Change a secret → new name → Deployment template changes → pods roll automatically.

🔐 **Critical truth:** Secrets are only **base64-encoded, not encrypted**. Anyone with read access can decode them.
Real protection = RBAC (who can read secrets) + encryption-at-rest in etcd + an external secret manager.
That's why prod uses **External Secrets Operator + Vault** ([externalsecret.yaml](../k8s/overlays/prod/externalsecret.yaml)):
the secret lives in Vault, ESO syncs it into the cluster, nothing secret is ever in Git.

Never paste secret values into chat, tickets or logs.

### 3.8 StatefulSet + PersistentVolume — for databases (Postgres, Qdrant)

**Why not a Deployment for Postgres?** Deployment pods are interchangeable with random names and no own disk.
A database needs:

| Need | StatefulSet gives |
|---|---|
| Stable identity | Names `postgres-0`, `qdrant-0` — same name after restart |
| Its own permanent disk | `volumeClaimTemplates` → one PVC per pod, **re-attached** to the same pod forever |
| Ordered start/stop | `-0` starts before `-1`, important for DB primaries/replicas |

**Storage chain — learn these 3 words:**
```
PVC (PersistentVolumeClaim)  = "I need 5Gi of disk"            <- the pod's request
PV  (PersistentVolume)       = an actual piece of disk          <- created to satisfy the claim
StorageClass                 = HOW disks are created            <- yours: rancher.io/local-path
                               (in AWS: gp3 EBS volumes)
```
```bash
kubectl get pvc
kubectl get pv
kubectl get storageclass
```
Your PVCs are `data-postgres-0` (5Gi) and `storage-qdrant-0` (10Gi).

**Proof that data survives pod death** (run on your cluster):
```bash
kubectl exec postgres-0 -- psql -U sentinel -d sentinel -tAc "select count(*) from kg_triples"   # 19
kubectl delete pod postgres-0
kubectl exec postgres-0 -- psql -U sentinel -d sentinel -tAc "select count(*) from kg_triples"   # still 19
```
Same name, same disk, same data.

**Important local detail — disks are pinned to nodes:**
```bash
kubectl get pv -o custom-columns='CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]'
```
```
CLAIM              NODE
data-postgres-0    desktop-worker
storage-qdrant-0   desktop-worker2
```
`local-path` disks live on one node's filesystem, so `postgres-0` can **only** run on `desktop-worker`
(Lab 8 shows what happens when that node goes away). In AWS, EBS volumes are pinned to an **availability zone** —
same idea, bigger scope. That's why production uses **managed databases** (RDS) or replicated DB clusters.

### 3.9 Probes — how Kubernetes knows your app is healthy

Your API has three ([deployment.yaml](../k8s/base/deployment.yaml)):

| Probe | Your config | If it fails | Question it answers |
|---|---|---|---|
| `startupProbe` | `/healthz`, every 2s, up to 30 times | keep waiting (up to 60s) | "Has the app finished booting?" |
| `readinessProbe` | `/readyz` (checks Qdrant + Postgres), every 5s | **removed from Service traffic** (not restarted) | "Can it serve requests right now?" |
| `livenessProbe` | `/healthz` (process only), every 10s | **container restarted** | "Is the process stuck/dead?" |

**The #1 interview question:** *"Why doesn't liveness check the database?"*
> If liveness checked Postgres and Postgres blipped, Kubernetes would **restart every API pod at once** — they'd
> come back, Postgres still down, restart again → a cascading failure that turns a DB hiccup into a full outage.
> Readiness is the right tool: pods stop receiving traffic, stay alive, and recover by themselves when the DB returns.

You'll prove this in Lab 4.

### 3.10 Resources, requests, limits, QoS

```yaml
resources:
  requests: { cpu: 250m, memory: 384Mi }   # what the scheduler RESERVES on a node
  limits:   { memory: 768Mi }              # hard ceiling
```
- **CPU units:** `1000m` = 1 core. `250m` = a quarter core.
- **requests** → used for **scheduling** ("does this node have 250m free?") and for HPA percentages.
- **limits** → enforced at runtime. Exceed **memory** limit → container killed: `OOMKilled`.
  Exceed **CPU** limit → throttled (slowed), not killed.
- **Why no CPU limit?** CPU throttling causes latency spikes for bursty request handlers. Common production
  practice: set CPU requests, set memory limits, skip CPU limits.

**QoS class** (`kubectl describe pod` shows it):

| QoS | When | Evicted first under pressure? |
|---|---|---|
| `BestEffort` | no requests/limits | yes — first |
| `Burstable` (yours) | requests < limits | middle |
| `Guaranteed` | requests == limits for CPU & memory | last |

```bash
kubectl top nodes
kubectl top pods
```
Your real numbers (idle):
```
NAME                            CPU(cores)   MEMORY(bytes)
postgres-0                      4m           27Mi
qdrant-0                        4m           417Mi
sentinel-rag-666d4d954d-bz4v5   5m           129Mi
valkey-5b9d54755b-g2dx8         7m           8Mi
```
(`kubectl top` needs **metrics-server** — installed and fixed on your cluster with `--kubelet-insecure-tls`,
which local clusters need because kubelet certificates are self-signed.)

### 3.11 HorizontalPodAutoscaler (HPA) — automatic scaling

[hpa.yaml](../k8s/base/hpa.yaml) (dev overlay: min 1, max 3):
```yaml
kind: HorizontalPodAutoscaler
spec:
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - resource: { name: cpu,    target: { averageUtilization: 65 } }   # % of the CPU *request*
    - resource: { name: memory, target: { averageUtilization: 75 } }
  behavior:
    scaleUp:   { stabilizationWindowSeconds: 0 }     # react immediately
    scaleDown: { stabilizationWindowSeconds: 300 }   # wait 5 min before removing pods (no flapping)
```
**The math:** `desiredReplicas = ceil(currentReplicas × currentUtilization / target)`.
Your load test hit 405%: `ceil(1 × 405 / 65) = 7` → capped at `maxReplicas: 3`.

**Real result from your cluster** (Lab 7):
```
cpu: 1%/65%    -> 1 pod
cpu: 405%/65%  -> scaled to 3 pods
pods landed on desktop-worker, desktop-worker2, desktop-worker3   <- spread across nodes
```

### 3.12 Scheduling — where pods land

The scheduler filters nodes (enough CPU/memory? taints? volume on this node?) then scores them. You influence it with:

| Tool | Your project | Meaning |
|---|---|---|
| **taints/tolerations** | control-plane `NoSchedule` | keep apps off the brain node |
| **topologySpreadConstraints** | spread by `kubernetes.io/hostname` (DoNotSchedule) and `topology.kubernetes.io/zone` (ScheduleAnyway) | don't put all API pods on one node/zone → survives a node or zone failure |
| volume node affinity | postgres PV on `desktop-worker` | the pod must go where its disk is |
| nodeSelector / affinity | (not used) | "only run on GPU nodes" etc. |

Your nodes have no zone labels (local cluster), so the zone rule is ignored ("ScheduleAnyway"); the hostname rule is
why your 3 pods landed on 3 different workers.

### 3.13 PodDisruptionBudget (PDB) — protection during maintenance

```yaml
kind: PodDisruptionBudget
spec:
  minAvailable: 2        # prod. Dev overlay patches to 0
```
**Voluntary disruptions** = things humans/tools do on purpose: `kubectl drain`, cluster upgrades, autoscaler
removing a node. A PDB says "never evict pods if that would leave fewer than 2 running."
It does **not** protect against crashes or a node suddenly dying (involuntary disruptions).

```bash
kubectl get pdb
```

### 3.14 NetworkPolicy — the cluster firewall

By default **every pod can talk to every pod** in the cluster. Your project locks that down
([networkpolicy.yaml](../k8s/base/networkpolicy.yaml)):

1. `default-deny-all` — block all traffic in and out of every pod in the namespace.
2. `sentinel-rag-api` — API may receive from ingress-nginx/monitoring namespaces; may send only to Qdrant,
   Postgres, Valkey, DNS, and HTTPS (blocking `169.254.169.254`, the cloud metadata endpoint — a classic SSRF target).
3. `datastores-from-api` — databases accept connections **only from API pods**.

**Proven on your cluster** — a pod in another namespace tries to reach Postgres:
```bash
kubectl run np-test -n default --rm -i --restart=Never --image=busybox:1.36 --command -- \
  sh -c 'nc -z -w 3 postgres.sentinel-rag.svc.cluster.local 5432 && echo REACHABLE || echo BLOCKED'
```
```
BLOCKED
```
…while the API pod connects fine (`api->postgres OK`). NetworkPolicies only work if the network plugin enforces
them — `kindnet` on your cluster does; in AWS EKS you enable it in the VPC CNI or use Calico/Cilium.

### 3.15 Pod Security & securityContext — least privilege

Your namespace enforces Pod Security **restricted**. Pods that could run as root or escalate privileges are
**rejected at the door**. Proven:
```bash
kubectl run psa-test --image=busybox:1.36 --dry-run=server -- sleep 5
```
```
Error from server (Forbidden): pods "psa-test" is forbidden: violates PodSecurity "restricted:latest":
allowPrivilegeEscalation != false ... unrestricted capabilities ... runAsNonRoot != true ...
```
Your API passes because of:
```yaml
securityContext:
  runAsNonRoot: true            # never root
  runAsUser: 10001              # matches USER 10001 in the Dockerfile
  seccompProfile: { type: RuntimeDefault }   # restrict system calls
containers:
  - securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true           # attacker can't write to the filesystem...
      capabilities: { drop: [ALL] }
    volumeMounts:
      - { name: tmp, mountPath: /tmp }       # ...except a small /tmp emptyDir
```

### 3.16 ServiceAccount — the pod's identity

`serviceaccount/sentinel-rag` with `automountServiceAccountToken: false`: the API doesn't need to call the
Kubernetes API, so it gets **no token** — if the pod is compromised, the attacker can't use it to control the
cluster. In AWS, you'd annotate this ServiceAccount with an IAM role (IRSA / Pod Identity) to grant cloud
permissions without access keys.

### 3.17 Ingress — HTTP routing from outside (inactive locally)

[ingress.yaml](../k8s/base/ingress.yaml) routes `sentinel.example.com` → Service `sentinel-rag`, with TLS,
rate limits and SSE settings. An Ingress is only **rules**; an **Ingress controller** (nginx, AWS Load Balancer
Controller) must be installed to act on them.
```bash
kubectl get ingressclass      # "No resources found" on your cluster -> no controller -> Ingress does nothing
```
That's why locally we use `port-forward`. In production: Internet → cloud load balancer → ingress controller → Service → pods.

### 3.18 Kustomize — one set of YAML, many environments

```
k8s/
  base/                 everything common (Deployment, Service, HPA, PDB, NetworkPolicy...)
  datastores/           qdrant, postgres, valkey
  overlays/
    dev/                base + all datastores, 1 replica, local image, secrets from secrets.env
    prod/               base + qdrant only, managed DBs, Vault secrets, real LLM, pinned image digest
```
An overlay **patches** the base instead of copying it. See the final YAML Kubernetes receives:
```bash
kubectl kustomize k8s/overlays/dev | less
kubectl kustomize k8s/overlays/prod | less
```
Compare them — that diff *is* the difference between your laptop and production.

---

## Part 4 — Follow one request through the cluster

You click **Execute** on `POST /v1/query` in Swagger:

```
1. Browser -> localhost:8000
2. kubectl port-forward tunnel -> Service sentinel-rag:80 -> one Ready API pod :8000
3. FastAPI: auth (X-API-Key hash from the Secret) -> rate limit (Valkey via Service "valkey")
4. LangGraph agent:
     dense + sparse search -> Service "qdrant" -> qdrant-0     (data on PVC storage-qdrant-0)
     knowledge graph       -> Service "postgres" -> postgres-0 (data on PVC data-postgres-0)
5. Every hop above is allowed by a NetworkPolicy; every name resolved by CoreDNS
6. Response + JSON log line (kubectl logs) + Prometheus metrics (/metrics)
```
In production, step 1–2 becomes: Internet → AWS load balancer → ingress-nginx → Service → pods (HPA 3–20).

---

## Part 5 — What `make k8s-up` does, line by line

[scripts/k8s_local.sh](../scripts/k8s_local.sh):

| Step | Command | K8s concept |
|---|---|---|
| Safety check | refuses unless context is local (docker-desktop, kind-*, minikube…) | never deploy to the wrong cluster |
| API key | writes only the **SHA-256 hash** to `.env` | secrets hygiene |
| Secrets file | random passwords → `k8s/overlays/dev/secrets.env` (git-ignored) | Kustomize `secretGenerator` input |
| Build | `docker build` → tag `sentinel-rag:local-<content-hash>` | **immutable tags** — nodes cache by tag, so a reused tag runs stale code |
| Apply | `kubectl kustomize … \| sed <pin tag> \| kubectl apply -f -` | declarative deploy, exactly one rolling update |
| Wait | `kubectl rollout status` for each workload | deploy isn't "done" until pods are Ready |

---

## Part 6 — Hands-on labs (do them in order)

> Keep a second terminal open running `kubectl get pods -o wide -w` (`-w` = watch live). It's like a live video of the cluster.

### Lab 1 — Explore (10 min)
```bash
kubectl get all
kubectl describe deploy sentinel-rag
kubectl get pod postgres-0 -o yaml | less        # the FULL object, incl. status Kubernetes added
kubectl explain deployment.spec.strategy          # built-in docs for any field
```
✅ You can explain: Deployment → ReplicaSet → Pod, and what `READY 1/1` means.

### Lab 2 — Self-healing (5 min)
```bash
kubectl delete pod -l app.kubernetes.io/component=api
```
Watch terminal 2: a new pod with a **new name** appears within seconds.
✅ Explain using the reconciliation loop (desired 1, actual 0 → create).

### Lab 3 — StatefulSet identity & persistence (5 min)
```bash
kubectl exec postgres-0 -- psql -U sentinel -d sentinel -tAc "select count(*) from kg_triples"
kubectl delete pod postgres-0
kubectl get pods -w                                # comes back as postgres-0 (same name!)
kubectl exec postgres-0 -- psql -U sentinel -d sentinel -tAc "select count(*) from kg_triples"
```
✅ Same count both times (yours: 19). Explain PVC → PV, and why Deployments can't do this.

### Lab 4 — Readiness vs liveness ⭐ (10 min)
```bash
kubectl scale statefulset postgres --replicas=0          # simulate a database outage
kubectl get pods -w                                       # API becomes 0/1 READY — but RESTARTS stays 0
kubectl get endpointslices -l kubernetes.io/service-name=sentinel-rag \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"  ready="}{.conditions.ready}{"\n"}{end}'
# -> every API pod shows ready=false: no traffic is sent to them
kubectl logs deploy/sentinel-rag --tail=5
kubectl scale statefulset postgres --replicas=1          # database returns
kubectl get pods -w                                       # API goes back to 1/1 by itself
```
Your real result:
```
during outage:   sentinel-rag-…-56kcr  0/1  Running  restarts=0   (all 3 pods)
after recovery:  sentinel-rag-…-bz4v5  1/1  Running  restarts=0
```
✅ You just demonstrated the #1 K8s interview answer. Explain why liveness must not check the DB.

### Lab 5 — Scale manually & watch spreading (5 min)
```bash
kubectl scale deploy sentinel-rag --replicas=3
kubectl get pods -o wide -l app.kubernetes.io/component=api      # 3 pods on 3 different workers
kubectl get endpointslices -l kubernetes.io/service-name=sentinel-rag \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"  ready="}{.conditions.ready}{"\n"}{end}'
# -> 3 IPs, all ready=true, behind one Service
```
✅ Explain topologySpreadConstraints. (The HPA will eventually scale it back to its own decision.)

### Lab 6 — Rolling update and rollback (10 min)
```bash
kubectl set env deploy/sentinel-rag LOG_LEVEL=WARNING     # imperative change to the pod template
kubectl rollout status deploy/sentinel-rag
kubectl rollout history deploy/sentinel-rag
kubectl rollout undo deploy/sentinel-rag                  # back to previous
```
Then run `make k8s-up` — any imperative change is replaced by what's in Git/YAML.
✅ Explain declarative vs imperative and why production uses GitOps (Argo CD) to prevent "drift".

### Lab 7 — Autoscaling under real load ⭐ (10 min)
Terminal 1:
```bash
make k8s-forward
```
Terminal 2:
```bash
.venv/bin/python scripts/load_test.py --seconds 120 --concurrency 40
```
Terminal 3:
```bash
kubectl get hpa sentinel-rag -w
```
Your real result: CPU went from `1%` → `405%` of request and the HPA scaled **1 → 3 pods**. After the test,
wait ~5 minutes (the `scaleDown` stabilization window) and watch it go back to 1.

Subtle point to mention in interviews: `port-forward` sends all traffic to **one** pod, so this lab tests the HPA,
not load balancing. Real traffic through a Service/Ingress is spread across all pods.
✅ Explain the HPA formula, why requests matter, and why scale-down is slow.

### Lab 8 — Node failure / maintenance (15 min)
```bash
kubectl get pods -o wide                                  # see which pods are on desktop-worker3
kubectl drain desktop-worker3 --ignore-daemonsets --delete-emptydir-data
kubectl get nodes                                         # desktop-worker3: Ready,SchedulingDisabled
kubectl get pods -o wide                                  # its pods were moved to other workers
kubectl uncordon desktop-worker3                          # allow scheduling again
```
Now the interesting one — drain the node holding Postgres's disk:
```bash
kubectl drain desktop-worker --ignore-daemonsets --delete-emptydir-data
kubectl get pods -o wide                                  # postgres-0 stays Pending!
kubectl describe pod postgres-0 | tail -5                 # "volume node affinity conflict"
kubectl uncordon desktop-worker                           # postgres-0 schedules again, data intact
```
✅ Explain cordon vs drain, PDBs, and why stateful workloads + node-local storage are a risk
(→ managed databases or replicated storage in production).

### Lab 9 — ConfigMap changes need a restart (5 min)
```bash
kubectl exec deploy/sentinel-rag -- printenv LOG_LEVEL                  # DEBUG
kubectl patch configmap sentinel-rag-config -p '{"data":{"LOG_LEVEL":"INFO"}}'
kubectl exec deploy/sentinel-rag -- printenv LOG_LEVEL                  # still DEBUG!
kubectl rollout restart deploy/sentinel-rag && kubectl rollout status deploy/sentinel-rag
kubectl exec deploy/sentinel-rag -- printenv LOG_LEVEL                  # INFO
```
✅ Explain why, and how hash-suffixed generators (like your Secret) solve it.

### Lab 10 — Debug a broken deployment (10 min)
```bash
kubectl set image deploy/sentinel-rag api=sentinel-rag:doesnotexist
kubectl get pods                                  # new pod: ErrImagePull / ImagePullBackOff
                                                  # OLD pod still Running -> users unaffected (maxUnavailable: 0)
kubectl describe pod <the-new-pod> | tail -8      # Events explain the failure
kubectl rollout undo deploy/sentinel-rag
```
✅ Explain why a bad deploy didn't cause downtime.

### Lab 11 — Network firewall (5 min)
Run the `np-test` command from section 3.14 → `BLOCKED`. Then:
```bash
kubectl exec deploy/sentinel-rag -- python -c "import socket;socket.create_connection(('postgres',5432),3);print('OK')"
```
✅ Explain default-deny and why the metadata IP `169.254.169.254` is blocked.

### Lab 12 — Security gate (2 min)
Run the `psa-test` command from section 3.15 → `Forbidden`.
✅ Explain Pod Security Standards and each line of your securityContext.

### Lab 13 — Inside the pods (10 min)
```bash
kubectl exec -it deploy/sentinel-rag -- sh
  id                       # uid=10001 -> not root
  touch /app-test          # fails: read-only filesystem
  touch /tmp/ok && echo ok # works: /tmp is an emptyDir
  exit
kubectl exec -it postgres-0 -- psql -U sentinel -d sentinel -c "\d kg_triples"
kubectl port-forward svc/qdrant 6333:6333        # then open http://localhost:6333/dashboard
```

---

## Part 7 — Troubleshooting playbook

**Always go wide → narrow:**
```
kubectl get pods -o wide                 1. which pod, what status, which node, restarts?
kubectl describe pod <pod>               2. read EVENTS at the bottom — answers 90% of problems
kubectl logs <pod> [--previous]          3. what did the app print? --previous = the crashed run
kubectl get events --sort-by=.lastTimestamp   4. cluster timeline
kubectl exec -it <pod> -- sh             5. test from inside (DNS, connectivity, files)
kubectl top pods                         6. resource usage
```

| Symptom | Likely cause | Fix |
|---|---|---|
| `Pending` | no node has enough CPU/memory; PVC unbound; volume on a cordoned node; taint | `describe pod` → Events |
| `ImagePullBackOff` / `ErrImagePull` | wrong image name/tag; image not available to the node | check image; `make k8s-up` |
| `CrashLoopBackOff` | app crashes at startup (bad config, missing env, exception) | `kubectl logs <pod> --previous` |
| `Running` but `0/1` READY | readiness failing (dependency down) | `curl localhost:8000/readyz` via port-forward; check DB pods |
| `OOMKilled` (in describe → Last State) | exceeded memory limit | raise `limits.memory` or fix the leak |
| `CreateContainerConfigError` | missing Secret/ConfigMap or key | `kubectl get secret,cm`; check `secrets.env` |
| `Error from server (NotFound)` | wrong namespace or old pod name | `-n sentinel-rag`; use `deploy/sentinel-rag` |
| `Forbidden … PodSecurity` | pod violates `restricted` | add securityContext fields from 3.15 |
| HPA `<unknown>` | metrics-server missing or not ready | install/patch metrics-server |
| port-forward stops working | the pod it was attached to was replaced | restart `make k8s-forward` |
| Changes "not applied" | same image tag cached / ConfigMap without restart | unique tags; `rollout restart` |

---

## Part 8 — kubectl cheat sheet

```bash
# context & namespace
kubectl config get-contexts
kubectl config use-context docker-desktop
kubectl config set-context --current --namespace=sentinel-rag

# look
kubectl get pods -o wide            kubectl get all             kubectl get pods -w
kubectl describe <kind> <name>      kubectl get <kind> <name> -o yaml
kubectl logs -f deploy/sentinel-rag kubectl logs <pod> --previous
kubectl top nodes                   kubectl top pods
kubectl get events --sort-by=.lastTimestamp
kubectl explain <kind>.spec         # docs for any field

# act
kubectl apply -k k8s/overlays/dev   kubectl delete -k k8s/overlays/dev
kubectl scale deploy/<name> --replicas=3
kubectl rollout status|history|undo|restart deploy/<name>
kubectl exec -it <pod> -- sh        kubectl port-forward svc/<svc> 8000:80
kubectl cordon|uncordon|drain <node>

# project shortcuts
make k8s-up   make k8s-forward   make k8s-status   make k8s-logs   make k8s-down
```

---

## Part 9 — Local vs production (AWS EKS)

| Concern | Your laptop | Production (e.g. EKS) |
|---|---|---|
| Control plane | kind container | managed by AWS (you never see etcd) |
| Nodes | 4 containers, fixed | EC2 nodes added/removed by Karpenter / Cluster Autoscaler |
| Traffic in | `port-forward` | Route 53 → ALB/NLB → ingress controller → Service |
| Images | local Docker, `:local-<hash>` | registry (ECR), pinned **digest**, Trivy-scanned in CI |
| Secrets | `secrets.env` → secretGenerator | Vault / AWS Secrets Manager → External Secrets Operator |
| Postgres / Valkey | in-cluster StatefulSet / Deployment | RDS/Aurora, ElastiCache (Multi-AZ, backups) |
| Qdrant | 1 replica, local-path disk | replicated, gp3 EBS, snapshots |
| API replicas | 1–3 | 3–20 across 3 availability zones |
| Deploys | `make k8s-up` | GitOps: merge PR → CI builds image → Argo CD syncs → canary (Argo Rollouts) |
| Monitoring | `kubectl top`, logs | Prometheus + Grafana / Datadog, alerts from `servicemonitor.yaml` |

---

## Part 10 — Kubernetes interview questions (with answers)

1. **What is a Pod vs a container?** A pod wraps one or more containers sharing network and storage; it's the
   unit K8s schedules. Containers in one pod are always on the same node.
2. **Deployment vs StatefulSet?** Deployment: identical, disposable, stateless replicas with random names.
   StatefulSet: stable names (`-0`), one persistent volume per pod, ordered rollout — for databases.
3. **Deployment vs ReplicaSet?** ReplicaSet keeps N pods; Deployment manages ReplicaSets to do rolling updates and rollbacks.
4. **How do pods find each other?** Services provide stable virtual IPs + DNS names via CoreDNS; pods are
   selected by labels.
5. **ClusterIP vs NodePort vs LoadBalancer vs Ingress?** See 3.5 and 3.17.
6. **Liveness vs readiness vs startup probes?** See 3.9 — and why liveness must not check dependencies.
7. **How does a rolling update achieve zero downtime?** `maxUnavailable: 0`, readiness gates traffic, preStop
   sleep for load-balancer deregistration, graceful shutdown, PDB during maintenance.
8. **Requests vs limits?** Requests reserve capacity and drive scheduling + HPA; limits cap usage. Memory over
   limit → OOMKilled; CPU over limit → throttled.
9. **How does the HPA decide?** `ceil(current × utilization / target)`, bounded by min/max, with stabilization windows.
10. **What happens when a node dies?** Node goes `NotReady`; after the toleration timeout (default 300s) its pods
    are evicted and recreated elsewhere by their controllers. Stateful pods need their volume to be reachable.
11. **How do you store secrets safely?** Not in Git; external secret manager + ESO/CSI driver; RBAC; etcd
    encryption at rest; never in logs. Base64 is not encryption.
12. **What's a NetworkPolicy?** A pod-level firewall by labels/namespaces; needs a CNI that enforces it; best
    practice is default-deny + explicit allows.
13. **What's a PDB?** Limits voluntary evictions (drain, upgrades) so availability stays above a floor.
14. **Taints/tolerations vs node affinity?** Taints repel pods from nodes (unless tolerated); affinity attracts
    pods to nodes.
15. **How do you debug CrashLoopBackOff?** `describe` → Events, `logs --previous`, check config/env, probes too
    aggressive?, OOMKilled?
16. **Why immutable image tags?** Reused tags (`:latest`) may not be re-pulled, rollbacks become ambiguous, and you
    can't tell what's running. Use unique tags or digests.
17. **ConfigMap changed but app didn't — why?** Env vars are read at start; restart or use hash-suffixed generators.
18. **What is etcd and why care?** The cluster's key-value database; losing it means losing cluster state → backups, HA (3/5 members).
19. **What does the scheduler consider?** Resource requests, taints, affinity, topology spread, volume location, then scoring.
20. **How would you scale this AI service to 10× traffic?** HPA on CPU (or custom metrics like requests in
    flight / queue depth via KEDA), cluster autoscaler for nodes, cache, LLM concurrency bulkheads, async
    ingestion queue, managed databases, multi-AZ spread.

---

## Part 11 — Glossary

| Term | One-line meaning |
|---|---|
| Cluster | A set of nodes managed as one by a control plane |
| Node | A machine (VM/server) that runs pods |
| Control plane | API server + etcd + scheduler + controller manager |
| kubelet | Node agent that runs pods and probes |
| Pod | Smallest deployable unit: container(s) sharing IP/storage |
| ReplicaSet | Keeps N identical pods running |
| Deployment | Manages ReplicaSets for rolling updates/rollbacks |
| StatefulSet | Like Deployment but stable identity + per-pod storage |
| DaemonSet | One pod on every node (agents, networking) |
| Job / CronJob | Run-to-completion task / on a schedule |
| Service | Stable IP/DNS + load balancing to ready pods |
| EndpointSlice | The list of pod IPs currently behind a Service |
| Ingress | HTTP routing rules from outside (needs a controller) |
| ConfigMap / Secret | Configuration / sensitive configuration |
| PVC / PV / StorageClass | Disk request / actual disk / how disks are made |
| Namespace | Folder-like scope for names, policies, quotas |
| Label / Selector | Tags on objects / queries that match tags |
| Probe | Health check: startup, readiness, liveness |
| HPA | Scales replicas by metrics |
| PDB | Minimum availability during voluntary disruptions |
| NetworkPolicy | Pod-level firewall rules |
| Taint / Toleration | Node "keep out" sign / pod's permission to ignore it |
| CNI | Network plugin giving pods IPs (kindnet, Calico, Cilium, AWS VPC CNI) |
| Kustomize | Base + overlays YAML customization built into kubectl |
| Reconciliation | Controllers continuously moving actual state toward desired state |

---

## Part 12 — 10-day plan (30–60 min/day)

| Day | Read | Do | You're done when you can... |
|---|---|---|---|
| 1 | Parts 1–2 | Lab 1 | Explain desired state + name the 4 control-plane components |
| 2 | 3.1–3.4 | Labs 2, 5 | Explain Pod → ReplicaSet → Deployment and labels/selectors |
| 3 | 3.5–3.7 | Lab 9, DNS command | Explain Services, DNS, ConfigMap vs Secret |
| 4 | 3.8 | Lab 3 | Explain StatefulSet, PVC/PV/StorageClass |
| 5 | 3.9 | Lab 4 ⭐ | Whiteboard liveness vs readiness with the cascading-failure story |
| 6 | 3.10–3.11 | Lab 7 ⭐ | Do the HPA math; explain requests/limits/QoS |
| 7 | 3.12–3.13 | Lab 8 | Explain scheduling, drain, PDB, node failure |
| 8 | 3.14–3.16 | Labs 11–13 | Explain default-deny, Pod Security, least privilege |
| 9 | 3.17–3.18, Parts 4–5 | Lab 6, Lab 10 | Explain rolling updates, rollback, Kustomize overlays |
| 10 | Parts 7, 9, 10 | Mock interview with Part 10 | Answer all 20 questions without notes |

**Final test:** reset everything and rebuild from scratch, explaining each step out loud:
```bash
make k8s-down
make k8s-up
```
