# ADR-0002: Sites are fully independent; there is no central hub

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** project owner
- **Related:** [HLD §5.1](../architecture/hld.md), [HLD §1.2](../architecture/hld.md)

---

## Context

The PRD describes deployments at "Primary Home, Parents Home, Business, Future
locations", with the same package deployed everywhere and only location
configuration differing. That wording is compatible with two very different
architectures:

1. **Independent sites** — each location runs its own complete stack, stores its own
   video locally, and shares nothing.
2. **Hub and spoke** — each location runs a capture node and reports to a central
   dashboard that aggregates health, alerts, and possibly footage.

The choice is the single largest fork in the design. Hub-and-spoke requires inbound
connectivity or outbound tunnels from every site, a central service that becomes a
single point of failure, authentication and authorisation across sites, and a data
residency question about where family or business footage lives.

The owner's position, stated directly during planning: *"Each deployment will have
independent data stored… Each deployment has their own local data which the videos
will be stored. No common pool."*

## Decision

**Each site is a complete, independent installation. There is no central hub, no
cross-site data sharing, no inbound connectivity requirement, and no shared storage.**

Consequences that follow from this and are therefore also decided:

- The state store is single-host SQLite, not a shared database ([ADR-0004](0004-sqlite-for-state.md))
- Each site has its own B2 bucket and its own credentials, scoped to that bucket
- Health is answered per site; there is no cross-site roll-up
- A future read-only status export is *possible* but explicitly not built

## Consequences

### Positive

- **No inbound network requirement.** A site needs only outbound access to B2 and to
  its notification service. It works behind CGNAT, on a mobile connection, or on a
  network the operator does not control
- **No central point of failure.** The parents' home keeps recording even if the
  primary home's machine, network, or power is down
- **Simplest possible security model.** No cross-site authentication, no credentials
  that grant access to more than one site's footage
- **Data locality.** Footage lives where it was recorded, plus in that site's own
  cloud bucket. This matters for a family home and will matter more for a business
- **Deployment simplicity.** Onboarding a new site is `install.sh`, with no
  registration step and no coordination
- The failure domain is exactly one site, which is also the trust domain

### Negative / accepted costs

- **No single view across sites.** An operator with three sites must open three
  dashboards. Accepted explicitly by the owner
- **No shared configuration.** Adding a camera to three sites means editing three
  `cameras.yml` files. A future "profile sync" tool would be a separate project
- **No cross-site redundancy.** A site's data exists in that site's local storage and
  that site's bucket. There is no third copy
- **Version drift between sites is possible.** Each site upgrades on its own schedule,
  so an operator may be running 1.2 at one location and 1.4 at another. Mitigated by
  the version being visible in the status page and in the doctor output
- **Cost is duplicated per site.** Each site needs its own host and its own bucket,
  though the cloud cost is small at clip-only retention

### Neutral

- Alerting is per site. An operator monitoring three sites configures notifications
  three times, or points all three at one ntfy topic — their choice, and it works
  because the alerting tool is per site but the destination need not be

## Alternatives considered

| Alternative | Why not |
|---|---|
| Hub with direct remote access to each site | Requires inbound access or a VPN mesh per site. Adds a central service, central auth, and a single point of failure for *visibility* if not for recording |
| Hub with sites pushing health only (no footage) | The most plausible middle ground. Rejected for v1 as unnecessary complexity, but explicitly left open: the status endpoint is already JSON, so pushing it somewhere later is additive and does not require redesign |
| Shared object storage across sites | Breaks the blast-radius and credentials-scoping benefits, and creates a shared failure domain for data that has no reason to be shared |
| A shared database across sites | Impossible with SQLite, and a shared Postgres would reintroduce every problem above |

## Revisit if

- The operator acquires enough sites that per-site dashboards become a real burden
  (the trigger is operator pain, not site count)
- A business requirement for cross-site search appears
- A site cannot make outbound connections at all, which would break the *cloud backup*
  half of this design rather than the independence half

**If revisited:** the intended direction is a read-only status push from each site to a
small aggregator, leaving recording, storage, and credentials fully independent. That
keeps every benefit listed above.
