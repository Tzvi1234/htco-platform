# Policy engine — Security

ASVS 5.0: 8.2.1 and 8.3.1 (authorization on the server, deny by default), 8.3.3 (an employee acts within the originating user's access), 8.4.1 (tenant isolation), 16.2.x (an audit trail of authorization decisions).

| Threat | Control |
|---|---|
| An unknown capability or identity slips through | Deny by default: no policy, a capability outside the catalogue, or an identity outside its section → `DENY` (AC-7.2.1) |
| An automation or employee borrows a user's rights | Their grants come only from their own section. A user principal attached to an automation request is never read. An employee gets only shop narrowing from the user, never capabilities (AC-7.2.4) |
| An advisor's answer authorizes an action | The document has no advisor section, and an advisor is denied before any grant is read (AC-7.2.5, ER-AI-17) |
| Cross-tenant use | A policy or shop of another organization → `DENY`. RLS: the Owner and Admins read only their own organization's rows; the worker only its job's |
| Rewriting history | Both tables are append-only, by trigger, at any role. Decisions name their version with a foreign key, and a published version never changes (AC-7.2.3) |
| Silent widening of grants | Publishing is Owner only, at `aal2` with a recent step-up ("Edit model policies"), and audited in the same transaction. No member can insert directly, and the worker never publishes. An external capability without a pre-approval needs an approval (ER-AI-04). A critical one always does |
| Personal data in the trail | A decision row holds identifiers, the capability, the verdict and the reason. It holds no request parameters, buyer data or free text. The capability is cut to 200 characters |
| Model-chosen tenant or actor | `PolicyRequest` takes the organization, actor and shop from the run or the authenticated context (ER-AI-12) |
