# Process backends

[Physical](physical/README.md) adapters model equipment/spatial execution; [biology](biology/README.md) adapters model soil-water/crop evolution. They are complementary process dimensions, not two alternative names for the same backend selector.

Adapters implement declared contracts, unit conversion, capability reporting and explicit rejection of unsupported behavior. The runtime selects and binds them, owns time and coordinates effects. Cross-backend coupling must define ownership of duration, energy, water and treatment effects to prevent duplicate settlement.

The CLI `--backend` selects biology. L1 supports Native/WOFOST without equipment. L3 explicitly composes Native season biology with the Native physical ledger; WOFOST L3 is rejected. No engine is silently substituted.
