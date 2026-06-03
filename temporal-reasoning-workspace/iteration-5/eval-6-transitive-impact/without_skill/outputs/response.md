# Eval 6 — Without Skill
## Tool calls
(none)
## Response to user
To assess what services might be affected by refactoring the key-store service's signing key format, I'd need to trace all upstream consumers of that service. Without access to a dependency graph or architectural documentation, I can only offer general guidance on how to investigate this.

**How to find affected services:**

1. **Search the codebase for imports/references to key-store**: Look for any service that imports from, calls, or depends on the key-store service. Common patterns to search for include the service name, its client library, its API endpoint paths, or its package name.

2. **Check API contracts**: If key-store exposes an API, find all callers of that API. The signing key format change may break consumers if they parse or validate the key format themselves.

3. **Review shared libraries or SDKs**: If key-store provides a shared client library used by other services, all services using that library would need updates.

**Likely affected services (general patterns):**

- **Authentication/authorization services** — These most commonly consume signing keys to verify tokens (e.g., JWT validation).
- **API gateways or middleware** — Often perform token verification at the edge using signing keys.
- **Any service that validates signed tokens or payloads** — If the format change alters how keys are serialized (e.g., PEM vs. JWK vs. raw bytes), all validators will need to handle the new format.

**Recommended steps:**

1. Run a code search for any reference to key-store (e.g., `grep -r "key-store\|key_store\|KeyStore" --include="*.py" .`).
2. Check service dependency manifests or infrastructure-as-code files for declared dependencies on key-store.
3. Review the key-store's API documentation or interface contracts to understand what the format change breaks.
4. Coordinate with teams owning downstream services before rolling out the change.

Without a recorded dependency map in memory, I cannot give a definitive list of affected services for your specific system. If you share the service topology or codebase structure, I can give a more precise answer.
