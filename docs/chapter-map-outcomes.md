# Chapter-Map Outcomes

Chapter-map refresh distinguishes insufficient coverage from a provider failure.
Provider order and identity resolution are unchanged.

## Insufficient Coverage

A validated successful response can contain no collected chapters, no confident
Kitsu match, or a map that fails the existing coverage and range checks. If every
attempted provider succeeded but no usable fallback exists, the source remains
`degraded`, with `outcome=insufficient_coverage`, zero consecutive failures, and
no failure retry deadline. The successful observation updates `last_success_at`;
it does not establish a new usable map. Normal scheduled or explicit refreshes
can check coverage again, subject to the series update strategy.

The overview does not label this condition as a provider outage. Refresh returns
false, and does not apply a sparse map or claim healthy coverage.

## Provider Failure

HTTP failures, timeouts, transport errors, invalid JSON, malformed structures,
incomplete capped pagination, and unexpected exceptions are failed observations.
A later failed pagination request discards that provider's partial result.

If no usable fallback exists, one failed provider is enough to record an aggregate
provider failure, even when another provider successfully returned insufficient
coverage. The existing failure count and retry policy apply once per refresh.

A usable MangaDex, Kitsu, or local CBZ fallback records aggregate success through
the existing selection and application rules. Attempted-provider outcomes remain
in source details; providers that were not attempted are not reported.

## Preservation

Empty, sparse, or failed observations never remove cached maps, selected
provenance, locks, manual metadata, local observations, or stored provider IDs.
The public map-fetch helpers still return dictionaries, and the refresh helper
still returns a boolean. No schema or public API changes are required.
