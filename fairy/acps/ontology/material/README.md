# Materials and inventories

Scope: material definitions, batches and located inventories, including seed, fertilizer, pesticide, fuel and water. Distinguish material type, stock instance, transfer and consumption. Electricity may require a supply/storage model rather than an ordinary material batch.

L1 has no material inventory; L3 has the ledger described below. General located-stock ontology objects and transfers beyond the selected grain flow are not yet implemented. The pilot irrigation quota is a usage limit, not a simulated water-source stock. Do not infer physical inventory from that quota or allow duplicate accounting in equipment and backend models.

## L3 material accounting

The full-season physical ledger tracks seeds, fuel, grain dry mass and grain
water separately. Stored batches retain measured moisture. Irrigation remains
a field-average usage quota; no water-source inventory is fabricated.
