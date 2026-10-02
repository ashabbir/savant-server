# Kubernetes Scaling & MCP Multi-Replica Deployment Guide

This guide explains how to scale **Savant Server** horizontally to multiple pod replicas in Kubernetes while running Model Context Protocol (MCP) services.

---

## Zero Client Configuration: Add MCP & Scale Transparently

End users and AI client tools do **not** have to do anything special to work with a multi-replica Savant deployment. They simply add the standard MCP endpoint to their configuration with their API key, exactly as they would with a single-instance server.

### Standard Client Configuration (Masked Key)

Add to client configuration (e.g., Claude Desktop, Cursor, Cline):

```json
{
  "mcpServers": {
    "savant-context": {
      "type": "http",
      "url": "http://<savant-service-or-ingress>:8193/mcp",
      "headers": {
        "X-API-Key": "sk-••••••••••••••••"
      }
    }
  }
}
```

Or via Copilot CLI:

```bash
copilot mcp add --transport http \
  --header "X-API-Key: sk-••••••••••••••••" \
  savant-context http://<savant-service-or-ingress>:8193/mcp
```

### Why It Scales Automatically Out of the Box

| Subsystem | What Happens Under the Hood | User / Developer Action |
| :--- | :--- | :--- |
| **Stateless Streamable HTTP** | Enabled by default (`SAVANT_MCP_STATELESS_HTTP=true`). No session locks are pinned to container memory. | **None** (Default behavior) |
| **Cross-Pod Request Routing** | Each JSON-RPC call is processed with a transient transport. Request 1 (`initialize`) can hit Pod 1 and Request 2 (`tools/call`) can hit Pod 2 without `404 Session not found`. | **None** (Works with standard round-robin K8s service `sessionAffinity: None`) |
| **Session & Auth Sync** | When a client initiates a session, credentials and session IDs are synchronized via PostgreSQL (`mcp_sessions`). Any replica can authenticate subsequent calls. | **None** (Automatic) |
| **Bundled Offline Models** | Both `stsb-distilbert-base` and `bge-reranker-base` are pre-baked in the Docker image with `SAVANT_OFFLINE_MODELS=1`. | **None** (Zero runtime downloads) |

---

## The Challenge with MCP in Multi-Replica Environments

Standard MCP over HTTP has two transports:
1. **Streamable HTTP (`/mcp`, ports 8191–8195)**
2. **SSE (`/sse`, ports 8091–8095)**

By default in the MCP specification, both transports can be **stateful**:
* When a client connects and initializes, the server generates an `Mcp-Session-Id` and creates an in-memory transport session.
* In a Kubernetes cluster with multiple replicas behind a standard Service or Ingress load-balancer, if a subsequent request (`tools/call` or `tools/list`) lands on a **different pod replica**, that replica will return:
  ```json
  {"jsonrpc": "2.0", "id": "server-error", "error": {"code": -32600, "message": "Session not found"}}
  ```
  with HTTP `404 Not Found`.

---

## Solutions Supported by Savant Server

Savant Server provides **two levels of support** for multi-replica scaling:

### Approach 1: Stateless Streamable HTTP (Recommended — Zero Sticky Sessions Needed)

All five MCP servers support running in **stateless mode**:
* **Environment Variable**: `SAVANT_MCP_STATELESS_HTTP=true` (default: `true`)
* **CLI Flag**: `--stateless`

#### How it works:
1. **Transient Transports**: Each request is evaluated independently as a standalone JSON-RPC operation. No in-memory session lock is stored.
2. **Any-Replica Routing**: Requests from the same client session can land on Pod 1, Pod 2, or Pod 3 interchangeably.
3. **Database-Backed Session Auth (`mcp_sessions`)**:
   When an MCP client sends an `Mcp-Session-Id` along with credentials (`X-API-Key`), Savant persists the mapping to PostgreSQL (`mcp_sessions` table) and caches it locally. If a follow-up request with that session ID hits another pod replica without repeating the API key, that replica automatically resolves the credentials from the shared database.
4. **Zero Affinity Requirement**: Works with standard round-robin Kubernetes Service load balancing (`sessionAffinity: None`).

---

### Approach 2: Stateful Session Affinity (Sticky Sessions)

If your clients rely on **SSE (`/sse`)** or stateful long-lived streaming connections:

#### A. Kubernetes Service Session Affinity (Client IP)
Route all traffic from the same client IP address to the same backend pod:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: savant-server
spec:
  type: ClusterIP
  selector:
    app: savant-server
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 10800 # 3 hours
  ports:
    - name: api
      port: 8090
      targetPort: 8090
    - name: mcp-workspace
      port: 8191
      targetPort: 8191
    - name: mcp-abilities
      port: 8192
      targetPort: 8192
    - name: mcp-context
      port: 8193
      targetPort: 8193
    - name: mcp-knowledge
      port: 8194
      targetPort: 8194
    - name: mcp-reminders
      port: 8195
      targetPort: 8195
```

#### B. Ingress-Nginx Sticky Session by `Mcp-Session-Id` Header
If using `ingress-nginx`, route requests based on the `Mcp-Session-Id` header so requests sharing the same session ID always reach the same pod:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: savant-mcp-ingress
  annotations:
    kubernetes.io/ingress.class: "nginx"
    # Route requests with the same Mcp-Session-Id to the same replica
    nginx.ingress.kubernetes.io/upstream-hash-by: "$http_mcp_session_id"
    # Fallback cookie affinity when no session ID is present yet
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/session-cookie-name: "mcp-route"
    nginx.ingress.kubernetes.io/session-cookie-hash: "sha1"
spec:
  rules:
    - host: savant.internal
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: savant-server
                port:
                  number: 8090
```

---

## Production Kubernetes Deployment Manifest Example

Below is a complete, production-ready manifest demonstrating:
- `replicas: 3`
- Pre-baked offline ML models (`/app/models/...`)
- Stateless MCP enabled (`SAVANT_MCP_STATELESS_HTTP=true`)
- Externalized job worker separation (`SAVANT_EXTERNAL_JOB_WORKER=1`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: savant-server
  labels:
    app: savant-server
spec:
  replicas: 3
  selector:
    matchLabels:
      app: savant-server
  template:
    metadata:
      labels:
        app: savant-server
    spec:
      containers:
        - name: savant-server
          image: ahmedshabbir/savant-server:latest
          imagePullPolicy: IfNotPresent
          env:
            - name: SAVANT_ROLE
              value: "server"
            - name: SAVANT_EXTERNAL_JOB_WORKER
              value: "1"
            - name: SAVANT_MCP_STATELESS_HTTP
              value: "true"
            - name: SAVANT_OFFLINE_MODELS
              value: "1"
            - name: EMBEDDING_MODEL_DIR
              value: "/app/models/stsb-distilbert-base/v1"
            - name: RERANKER_MODEL_DIR
              value: "/app/models/bge-reranker-base/v1"
            - name: SAVANT_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: savant-secrets
                  key: database-url
          ports:
            - containerPort: 8090 # REST API
            - containerPort: 8191 # Workspace MCP (Streamable HTTP)
            - containerPort: 8192 # Abilities MCP (Streamable HTTP)
            - containerPort: 8193 # Context MCP (Streamable HTTP)
            - containerPort: 8194 # Knowledge MCP (Streamable HTTP)
            - containerPort: 8195 # Reminders MCP (Streamable HTTP)
          resources:
            requests:
              cpu: "1000m"
              memory: "3Gi"
            limits:
              cpu: "2000m"
              memory: "6Gi"
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8090
            initialDelaySeconds: 15
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8090
            initialDelaySeconds: 15
            periodSeconds: 10
```

---

## Kubernetes Service Definition (Stateless Round-Robin)

When `SAVANT_MCP_STATELESS_HTTP=true`, no session affinity is required. You can deploy a standard `ClusterIP` Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: savant-server
  labels:
    app: savant-server
spec:
  type: ClusterIP
  selector:
    app: savant-server
  sessionAffinity: None # No sticky sessions needed!
  ports:
    - name: api
      port: 8090
      targetPort: 8090
    - name: mcp-workspace
      port: 8191
      targetPort: 8191
    - name: mcp-abilities
      port: 8192
      targetPort: 8192
    - name: mcp-context
      port: 8193
      targetPort: 8193
    - name: mcp-knowledge
      port: 8194
      targetPort: 8194
    - name: mcp-reminders
      port: 8195
      targetPort: 8195
```

---

## Production Pre-Flight Checklist Before Going Live

Before scaling to multiple replicas in Kubernetes, verify:

1. **Database Schema Migration**:
   Ensure PostgreSQL migration 16 (`mcp_sessions` table) has been applied. This allows all replicas to validate session tokens and client credentials across pods:
   ```sql
   CREATE TABLE IF NOT EXISTS mcp_sessions (
       session_id VARCHAR(128) PRIMARY KEY,
       user_id VARCHAR(64) NOT NULL,
       api_key VARCHAR(128) NOT NULL,
       client_ip VARCHAR(64),
       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   ```

2. **Offline ML Models Bundled in Image**:
   Verify that Docker images were built with models baked in, and `SAVANT_OFFLINE_MODELS=1` is set. Pods must never attempt to download HuggingFace models from external networks at runtime.
   - Embedding: `/app/models/stsb-distilbert-base/v1`
   - Reranker: `/app/models/bge-reranker-base/v1`

3. **Memory Limits & Requests**:
   Each pod replica runs in-process PyTorch models. Allocate at least:
   - Requests: `cpu: 1000m`, `memory: 3Gi`
   - Limits: `cpu: 2000m`, `memory: 6Gi`

4. **Background Job Separation**:
   In multi-replica setups, ensure only one instance or a dedicated job pod runs background workers (`SAVANT_EXTERNAL_JOB_WORKER=1`), or rely on the PostgreSQL advisory locks already implemented in `knowledge.maintenance_runner`.

