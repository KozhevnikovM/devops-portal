# Computational Resources Booking System Concept

## 1. Overall Vision
**The system is a Self-Service Portal for infrastructure lifecycle management.**
The main goal is to replace manual resource requests with an automated "booking" process, where the user independently selects configuration and usage duration.

**Key principles:**
* **Transparency:** All users can see who has booked which resources.
* **Temporary by default:** Every resource has a TTL (Time-to-Live). Permanent resources require periodic confirmation.
* **Automation:** Minimizing DevOps involvement in the routine creation of VMs, Databases, and Namespaces.
* **Fairness:** Power distribution through a multi-level quota system.

---

## 2. User Roles and Stories

### 🛠 DevOps (Administrator)
* **Resource Catalog:** Creates and edits VM and DB templates so users can choose from approved configurations.
* **Limit Management:** Sets quotas at the team and project levels.
* **K8s Management:** Manages the list of available Namespaces for booking. *(Delivered in v0.5.0 — see Implementation Status.)*
* **Control:** See the overall resource map and can forcibly release resources when necessary.

### 🤖 Jenkins Service Account (Automation)
* **Dynamic Tests:** Books a clean environment (VM + DB) via API for a pipeline and receives access credentials.
* **Auto-Cleanup:** Sends a resource deletion request immediately after tests complete.
* **Refresh:** Recreates resources from new images within a single booking.

### 💻 Developers and QA (Users)
* **Quick Start:** Books a resource from a template (image + configuration scripts) and receives it within minutes.
* **Lifecycle:** Extends the life of their booking or deletes it manually.
* **Collaboration:** "Shares" their environment with colleagues for joint debugging.
* **Discovery:** Finds active bookings in their team to reuse existing environments.

---

## 3. Functional Features

### 🔄 Resource Lifecycle
`Booking` → `Deployment (Terraform/API)` → `Usage` → `Release`.

### 📦 Environments
Ability to group different resource types into a single logical stack.
*For example: The "Feature-X" environment includes 1 VM, 1 Database, and 1 K8s Namespace.*

### 📈 Quota Management
Multi-level quota system:
* **User level** → **Team level** → **Project level**.
* Limits can be quantitative (number of VMs) or resource-based (CPU/RAM).

### 🤝 Sharing Mechanism
* **Owner:** Has full rights (deletion, extension, access modification).
* **User:** Can use the resource but cannot affect its lifecycle.

### 📋 Templates and Configuration
* Library of predefined images.
* Ability to attach configuration scripts (Ansible, Bash) that run automatically after VM creation.

### ⏳ Keep-alive for Permanent Resources
For resources marked as "Permanent," a mandatory confirmation mechanism is introduced: the owner must confirm the resource is still needed at regular intervals, or it will be automatically removed.

---

## 4. Non-Functional Features

* **UX/UI:** Unified Dashboard with status visualization and countdown timers to the end of each booking.
* **Security:** Role-based access control (RBAC). Strict resource isolation between different users/projects.
* **Reliability:**
    * Queue-based resource creation (if external systems are temporarily unavailable).
    * Full audit log of all actions (who, when, and what created/changed/deleted).

---

## Implementation Status

A snapshot of what the concept above is **actually delivered** vs. still roadmap (as of v0.5.0).

**Delivered**
* **VM booking** end-to-end via Terraform/VMware: image + hardware catalog, TTL, per-user resource quota (CPU/RAM/disk), live status, audit log, admin force-delete.
* **Kubernetes namespaces (v0.5.0):** DevOps registers pre-created namespaces in an admin-managed **pool**; a user **reserves** an available one for a TTL from the *Namespaces* page; release or TTL expiry returns it to the pool. Namespaces are **reserved, not provisioned** — the portal does not create namespaces, run Terraform for them, or issue credentials.
* **Per-resource-type navigation** (Virtual Machines / Namespaces) with type-scoped booking lists.

**Roadmap (not yet built)**
* **Environments** — grouping several resources (e.g. 1 namespace + 2 VMs) into one order/stack.
* **Databases** as a resource type.
* **Namespace provisioning & credentials** — dynamic creation and a scoped kubeconfig (today namespaces are reserve-from-pool only).
* **Sharing**, and **team/project-level quotas** — quota is currently per-user.
* **Keep-alive** confirmation for permanent resources.
