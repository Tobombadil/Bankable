"""The CRM adapter package (docs/34-crm-system-of-record.md): `AttioCrmAdapter` (attio.py) and the
in-memory fake (fake.py) both implement `services.sor.ports.CrmPort`; `router.py` mounts the
inbound Attio webhook and the US-403 lead hand-off route; `signals.py` maps platform events to
`LeadSignal` rows. See `services/crm/README.md` for the full contract, decisions and deferred work.
"""

from __future__ import annotations
